"""
GDC Tender Monitor — Targeted Search & Scraper Edition
======================================================
Watches Kenyan procurement portals and runs targeted search queries
matching GDC's service lines: event management/design, AV & technical
production, simultaneous interpretation equipment (SIE), registration/ticketing,
branding & creative, PR & comms, data viz, consultancy.

MONITORING METHODS:
1. Google Custom Search API: Targeted queries against primary domains
   (US Embassy, NMK, universities, PPIP) while excluding login-gated aggregators.
2. Playwright PPIP Scraper: Renders client-side JS tables at tenders.go.ke.
3. Card-Grid Scraper: Structured card extraction (e.g. Amref).
4. Generic Link Scraper: Generalized link checking for UN/portal sites.

OUTPUT: Dated PDF in ./tender_reports/ with "New Matches" and "Repeated" sections.
"""

import json
import logging
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# Anchor all file output to the folder this script lives in
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 1. CONFIG — Credentials, Queries, and Portals
# ---------------------------------------------------------------------------

# API Credentials
# --- local secrets: read from secrets.env (kept off GitHub) or the environment ---
def _load_secrets(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets.env")):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_secrets()
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
SEARCH_ENGINE_ID = os.environ.get("SEARCH_ENGINE_ID", "")

# Targeted search queries designed to hit primary sources and bypass login-gated aggregators
TARGETED_SEARCH_QUERIES = [
    # US Embassy Kenya direct notice check
    'site:ke.usembassy.gov ("event" OR "conference" OR "summit" OR "RFQ" OR "logistics" OR "audio visual")',
    
    # National Museums of Kenya procurement & notices
    'site:nmk.go.ke ("tender" OR "rfq" OR "event" OR "conference" OR "symposium")',
    
    # Direct Kenyan university RFQs (Graduations, conferences, AV, sound)
    'site:.ac.ke ("tender" OR "rfq" OR "quotation") ("event" OR "conference" OR "sound system" OR "public address" OR "videography")',
    
    # Official Government Portal direct matches
    'site:tenders.go.ke ("event management" OR "audio visual" OR "conference facility" OR "simultaneous interpretation")',
    
    # Broad Kenya event RFQs excluding paywalled/login-gated aggregator directories
    '("Nairobi" OR "Kenya") ("event management" OR "conference logistics" OR "audio visual" OR "PA system") ("tender" OR "RFQ" OR "RFP") -site:tenderskenya.com -site:tenderyetu.com -site:tendersinfo.com',
]

PORTALS = [
    {
        "name": "PPIP (tenders.go.ke)",
        "url": "https://tenders.go.ke/tenders",
        "row_selector": "table tbody tr",
        "tender_no_col": 1,
        "description_col": 2,
        "entity_col": 3,
        "deadline_col": 6,
        "closes_in_col": 7,
        "description_search_selectors": [
            "input[placeholder*='Search D' i]",
            "input[placeholder*='description' i]",
            "thead input, .v-table thead input",
        ],
        "description_search_position": 2,
    },
]

CARD_GRID_PORTALS = [
    {
        "name": "Amref Health Africa",
        "url": "https://amref.org/tenders/",
        "card_selector": "ul.tender-grid li.tender-card",
        "title_selector": "h2, h3, h4",
        "link_selector": "a",
    },
]

GENERIC_SITES = [
    {"name": "UNGM (UN Global Marketplace)",
     "url": "https://www.ungm.org/Public/Notice?countryCode=KE"},
    {"name": "UNDP Procurement Notices",
     "url": "https://procurement-notices.undp.org/search.cfm"},
    {"name": "eGP Kenya",            "url": "https://egpkenya.go.ke"},
    {"name": "ReliefWeb (Kenya)",    "url": "https://reliefweb.int/updates?search=Kenya"},
    {"name": "Devex Jobs & Tenders", "url": "https://www.devex.com/jobs"},
]

# Keywords pulled directly from GDC's service lines
STRONG_KEYWORDS = [
    "event management", "event design", "stage design", "audio visual",
    "av production", "livestream", "live stream", "streaming",
    "simultaneous interpretation", "interpretation equipment",
    "interpreter booth", "sound system", "pa system", "led screen",
    "hospitality services", "videography", "corporate identity",
]

GENERIC_KEYWORDS = [
    "consultancy", "registration", "branding", "graphic design",
    "website development", "photography", "documentary",
    "public relations", "media relations", "communications strategy",
    "data visualization", "dashboard", "m&e", "monitoring and evaluation",
    "ticketing", "rsvp", "booth", "interpreter", "sie",
]

EVENT_CONTEXT_WORDS = [
    "event", "conference", "summit", "forum", "exhibition", "expo",
    "workshop", "seminar", "gala", "convention", "symposium",
]

TARGET_KEYWORDS = STRONG_KEYWORDS + [
    "conference", "summit", "forum", "exhibition", "gala",
]

DB_PATH = os.path.join(SCRIPT_DIR, "gdc_tenders.db")
PDF_OUTPUT_DIR = os.path.join(SCRIPT_DIR, "tender_reports")


# ---------------------------------------------------------------------------
# 2. STORAGE
# ---------------------------------------------------------------------------

def setup_database():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tenders (
            id TEXT PRIMARY KEY,
            portal TEXT,
            title TEXT,
            deadline TEXT,
            link TEXT,
            matched_keywords TEXT,
            first_seen TEXT
        )
    ''')
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 3. KEYWORD MATCHING
# ---------------------------------------------------------------------------

def match_keywords(text):
    """Two-tier keyword matcher. Strong keywords match independently;
    generic keywords require an event context word."""
    text_lower = text.lower()
    strong_hits = [kw for kw in TARGET_KEYWORDS if kw in text_lower]
    generic_hits = [kw for kw in GENERIC_KEYWORDS if kw in text_lower]
    has_event_context = any(ctx in text_lower for ctx in EVENT_CONTEXT_WORDS)
    confirmed_generic_hits = generic_hits if has_event_context else []
    return list(set(strong_hits + confirmed_generic_hits))


# ---------------------------------------------------------------------------
# 4. GOOGLE CUSTOM SEARCH API SCANNER
# ---------------------------------------------------------------------------

def scan_google_custom_search(query, api_key, cx, num_results=10):
    """Queries the Google Custom Search JSON API and keyword-matches
    the returned title and snippets."""
    if not api_key or api_key == "YOUR_GOOGLE_CLOUD_API_KEY":
        logging.warning("Google API Key not configured. Skipping targeted search.")
        return []

    base_url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cx,
        "q": query,
        "num": min(num_results, 10),
    }

    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    logging.info("Running Google targeted search: %s", query[:60])

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logging.error("Google Custom Search API error: %s", e)
        return []

    items = data.get("items", [])
    hits = []

    for item in items:
        title = item.get("title", "").strip()
        snippet = item.get("snippet", "").strip()
        link = item.get("link", "").strip()
        combined_text = f"{title} {snippet}"

        matched = match_keywords(combined_text)
        if not matched:
            # Fallback: if targeted search query already constrained the context,
            # capture query context as hit if at least one service word matched
            matched = [kw for kw in (STRONG_KEYWORDS + GENERIC_KEYWORDS) if kw in combined_text.lower()]
            if not matched:
                continue

        # Extract possible deadline date from snippet if available
        date_match = re.search(
            r"\b(?:deadline|closing|due|by)?\s*(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})\b",
            snippet,
            re.IGNORECASE,
        )
        deadline = date_match.group(0) if date_match else "See listing link"

        hits.append({
            "id": link,
            "portal": f"Targeted Search ({item.get('displayLink', 'Web')})",
            "title": title,
            "entity": item.get("displayLink", ""),
            "deadline": deadline,
            "link": link,
            "matched_keywords": ", ".join(matched),
        })

    logging.info("  Found %d match(es) for query", len(hits))
    return hits


# ---------------------------------------------------------------------------
# 5. PLAYWRIGHT SCRAPERS
# ---------------------------------------------------------------------------

def load_portal(page, portal):
    page.goto(portal["url"], wait_until="networkidle", timeout=30000)
    page.wait_for_selector(portal["row_selector"], timeout=15000)
    page.wait_for_timeout(1000)


def try_increase_page_size(page):
    try:
        select = page.query_selector("select")
        if not select:
            return
        options = select.query_selector_all("option")
        if not options:
            return
        best_value, best_num = None, -1
        for opt in options:
            text = opt.inner_text().strip()
            if text.lower() == "all":
                best_value, best_num = opt.get_attribute("value"), float("inf")
                break
            digits = re.sub(r"[^\d]", "", text)
            if digits and int(digits) > best_num:
                best_value, best_num = opt.get_attribute("value"), int(digits)
        if best_value is not None:
            select.select_option(best_value)
            page.wait_for_timeout(1000)
    except Exception as e:
        logging.info("  Could not adjust rows-per-page (non-fatal): %s", e)


def go_to_next_page(page):
    next_btn = page.query_selector(
        "button[aria-label*='Next' i], button[aria-label*='next page' i], "
        ".v-pagination__next button, button.v-pagination__next"
    )
    if not next_btn:
        return False
    is_disabled = next_btn.get_attribute("disabled") is not None
    aria_disabled = next_btn.get_attribute("aria-disabled")
    if is_disabled or aria_disabled == "true":
        return False
    next_btn.click()
    page.wait_for_timeout(1000)
    return True


def row_cell_text(row, col_index):
    cell = row.query_selector(f"td:nth-child({col_index})")
    return cell.inner_text().strip() if cell else ""


def extract_all_tenders(page, portal):
    rows = page.query_selector_all(portal["row_selector"])
    tenders = []
    for row in rows:
        description = row_cell_text(row, portal["description_col"])
        if not description:
            continue

        tender_no = row_cell_text(row, portal["tender_no_col"])
        entity = row_cell_text(row, portal["entity_col"])
        deadline = row_cell_text(row, portal["deadline_col"])
        closes_in = row_cell_text(row, portal["closes_in_col"])

        tender_id = tender_no or str(hash(portal["name"] + description))

        tenders.append({
            "id": tender_id,
            "portal": portal["name"],
            "title": description,
            "entity": entity,
            "deadline": f"{deadline} ({closes_in})" if closes_in else deadline,
            "link": f"{portal['url']} — Tender No: {tender_no}",
        })
    return tenders


def scan_portal(page, portal, max_pages=50):
    load_portal(page, portal)
    try_increase_page_size(page)

    all_tenders = []
    seen_ids = set()
    page_num = 1

    while page_num <= max_pages:
        page_tenders = extract_all_tenders(page, portal)
        new_count = 0
        for tender in page_tenders:
            if tender["id"] not in seen_ids:
                seen_ids.add(tender["id"])
                all_tenders.append(tender)
                new_count += 1

        if new_count == 0 and page_num > 1:
            break
        if not go_to_next_page(page):
            break
        page_num += 1

    matches = {}
    for tender in all_tenders:
        hits = match_keywords(tender["title"])
        if hits:
            tender["matched_keywords"] = ", ".join(hits)
            matches[tender["id"]] = tender

    return list(matches.values())


def scan_generic_site(page, site, max_items=100):
    try:
        page.goto(site["url"], wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1000)
    except Exception as e:
        logging.warning("  Could not load %s: %s", site["name"], e)
        return []

    try:
        links = page.query_selector_all("a")
    except Exception as e:
        logging.warning("  Could not read links for %s: %s", site["name"], e)
        return []

    base_match = re.match(r"https?://[^/]+", site["url"])
    base = base_match.group(0) if base_match else ""

    hits = {}
    checked = 0
    for link in links:
        if checked >= max_items:
            break
        try:
            text = link.inner_text().strip()
        except Exception:
            continue
        if not text or len(text) < 8:
            continue
        checked += 1

        matched = match_keywords(text)
        if not matched:
            continue

        href = link.get_attribute("href") or ""
        if href.startswith("/"):
            href = base + href
        elif not href.startswith("http"):
            continue

        hit_id = href or f"{site['url']}#{text[:80]}"
        if hit_id in hits:
            continue

        hits[hit_id] = {
            "id": hit_id,
            "portal": site["name"],
            "title": text,
            "entity": "",
            "deadline": "",
            "link": href or site["url"],
            "matched_keywords": ", ".join(matched),
        }

    return list(hits.values())


def scan_card_grid_portal(page, portal, max_items=100):
    try:
        page.goto(portal["url"], wait_until="networkidle", timeout=30000)
        page.wait_for_selector(portal["card_selector"], timeout=15000)
        page.wait_for_timeout(1000)
    except Exception as e:
        logging.warning("  Could not load %s: %s", portal["name"], e)
        return []

    cards = page.query_selector_all(portal["card_selector"])
    base_match = re.match(r"https?://[^/]+", portal["url"])
    base = base_match.group(0) if base_match else ""

    hits = []
    for card in cards[:max_items]:
        title_el = card.query_selector(portal["title_selector"])
        title = title_el.inner_text().strip() if title_el else card.inner_text().strip()[:150]
        if not title:
            continue

        matched = match_keywords(title)
        if not matched:
            continue

        link_el = card.query_selector(portal["link_selector"])
        href = link_el.get_attribute("href") if link_el else ""
        if href and href.startswith("/"):
            href = base + href

        card_id = href or f"{portal['url']}#{title[:80]}"
        card_text = card.inner_text()
        date_match = re.search(
            r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}",
            card_text,
        )
        deadline = date_match.group(0) if date_match else ""

        hits.append({
            "id": card_id,
            "portal": portal["name"],
            "title": title,
            "entity": "",
            "deadline": deadline,
            "link": href or portal["url"],
            "matched_keywords": ", ".join(matched),
        })

    return hits


# ---------------------------------------------------------------------------
# 6. ALERTING & PDF GENERATION
# ---------------------------------------------------------------------------

def send_alert(tender):
    logging.info(
        "🚨 NEW RFP [%s] %s | Deadline: %s | Matched: %s | %s",
        tender["portal"], tender["title"], tender["deadline"],
        tender["matched_keywords"], tender["link"],
    )


def _urgency_color(deadline_text, colors):
    if not deadline_text:
        return colors.black
    text = deadline_text.lower()
    match = re.search(r"closes in (\d+)", text)
    if "today" in text or "tomorrow" in text:
        return colors.HexColor("#C8102E")
    if match:
        days = int(match.group(1))
        if days <= 3:
            return colors.HexColor("#C8102E")
        if days <= 7:
            return colors.HexColor("#E07B00")
    return colors.black


def _build_tender_table(tenders, header_bg_hex, cell_style, header_style, styles):
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Table, TableStyle
    from reportlab.lib.styles import ParagraphStyle

    table_data = [[
        Paragraph("Portal", header_style),
        Paragraph("Tender / Description", header_style),
        Paragraph("Entity", header_style),
        Paragraph("Deadline", header_style),
        Paragraph("Matched Keywords", header_style),
        Paragraph("Where to find it", header_style),
    ]]

    for t in tenders:
        deadline_text = t.get("deadline", "")
        deadline_style = ParagraphStyle(
            "deadlineCell", parent=cell_style,
            textColor=_urgency_color(deadline_text, colors),
            fontName="Helvetica-Bold",
        )
        table_data.append([
            Paragraph(t.get("portal", ""), cell_style),
            Paragraph(t.get("title", ""), cell_style),
            Paragraph(t.get("entity", ""), cell_style),
            Paragraph(deadline_text, deadline_style),
            Paragraph(t.get("matched_keywords", ""), cell_style),
            Paragraph(t.get("link", ""), cell_style),
        ])

    col_widths = [1.0 * inch, 1.9 * inch, 1.0 * inch, 0.9 * inch, 1.3 * inch, 1.4 * inch]
    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_bg_hex)),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FBEAEC")]),
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.HexColor(header_bg_hex)),
    ]))
    return tbl


def generate_pdf_report(new_tenders, repeated_tenders=None):
    repeated_tenders = repeated_tenders or []

    if not new_tenders and not repeated_tenders:
        logging.info("No tenders (new or repeated) this run — skipping PDF.")
        return None

    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filepath = os.path.join(PDF_OUTPUT_DIR, f"gdc_tender_alerts_{stamp}.pdf")

    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8, leading=10)
    header_style = ParagraphStyle(
        "cellHeader", parent=styles["Normal"], fontSize=8, leading=10,
        textColor=colors.white, fontName="Helvetica-Bold",
    )
    title_style = ParagraphStyle("gdcTitle", parent=styles["Title"], textColor=colors.HexColor("#C8102E"))
    section_new_style = ParagraphStyle("sectionNew", parent=styles["Heading2"], textColor=colors.HexColor("#C8102E"))
    section_repeat_style = ParagraphStyle("sectionRepeat", parent=styles["Heading2"], textColor=colors.HexColor("#777777"))
    subtitle_style = ParagraphStyle("gdcSubtitle", parent=styles["Normal"], textColor=colors.HexColor("#555555"))

    doc = SimpleDocTemplate(
        filepath, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
    )
    story = []

    story.append(Paragraph("GDC Tender Alert Report", title_style))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%d %b %Y, %H:%M')} — "
        f"{len(new_tenders)} new match(es), {len(repeated_tenders)} repeated",
        subtitle_style,
    ))
    story.append(Spacer(1, 16))

    if new_tenders:
        story.append(Paragraph("New Matches", section_new_style))
        story.append(Spacer(1, 6))
        story.append(_build_tender_table(new_tenders, "#C8102E", cell_style, header_style, styles))
        story.append(Spacer(1, 20))

    if repeated_tenders:
        story.append(Paragraph("Repeated (seen in a previous run)", section_repeat_style))
        story.append(Spacer(1, 6))
        story.append(_build_tender_table(repeated_tenders, "#777777", cell_style, header_style, styles))

    doc.build(story)
    logging.info("PDF report written to %s", filepath)
    return filepath


# ---------------------------------------------------------------------------
# 7. MAIN EXECUTION
# ---------------------------------------------------------------------------

def process_hits(hits, cursor, conn, new_list, repeated_list):
    for hit in hits:
        cursor.execute("SELECT id FROM tenders WHERE id = ?", (hit["id"],))
        if cursor.fetchone():
            repeated_list.append(hit)
            continue
        send_alert(hit)
        new_list.append(hit)
        cursor.execute(
            """INSERT INTO tenders
               (id, portal, title, deadline, link, matched_keywords, first_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (hit["id"], hit["portal"], hit["title"],
             hit["deadline"], hit["link"], hit["matched_keywords"],
             datetime.now().isoformat()),
        )
        conn.commit()


def run():
    conn = setup_database()
    cursor = conn.cursor()
    new_tenders_this_run = []
    repeated_tenders_this_run = []

    # --- PART 1: Google Custom Search API (Targeted Searches) ---
    logging.info("=== Running Google Targeted Searches ===")
    for query in TARGETED_SEARCH_QUERIES:
        search_hits = scan_google_custom_search(query, GOOGLE_API_KEY, SEARCH_ENGINE_ID)
        process_hits(search_hits, cursor, conn, new_tenders_this_run, repeated_tenders_this_run)

    # --- PART 2: Playwright Scrapers (Direct Portals) ---
    logging.info("=== Running Browser Scrapers ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for portal in PORTALS:
            logging.info("Checking %s ...", portal["name"])
            try:
                tenders = scan_portal(page, portal)
            except Exception as e:
                logging.error("Failed to scan %s: %s", portal["name"], e)
                continue
            logging.info("  %d relevant tenders found on this run", len(tenders))
            process_hits(tenders, cursor, conn, new_tenders_this_run, repeated_tenders_this_run)

        for portal in CARD_GRID_PORTALS:
            logging.info("Checking %s (card-grid scan) ...", portal["name"])
            try:
                hits = scan_card_grid_portal(page, portal)
            except Exception as e:
                logging.error("Failed to scan %s: %s", portal["name"], e)
                continue
            logging.info("  %d relevant tenders found on this run", len(hits))
            process_hits(hits, cursor, conn, new_tenders_this_run, repeated_tenders_this_run)

        for site in GENERIC_SITES:
            logging.info("Checking %s (generic link scan) ...", site["name"])
            try:
                hits = scan_generic_site(page, site)
            except Exception as e:
                logging.error("Failed to scan %s: %s", site["name"], e)
                continue
            process_hits(hits, cursor, conn, new_tenders_this_run, repeated_tenders_this_run)

        browser.close()

    conn.close()
    generate_pdf_report(new_tenders_this_run, repeated_tenders_this_run)
    logging.info("Run complete.")


if __name__ == "__main__":
    run()