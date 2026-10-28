"""
GDC Tender Monitor — Prototype
================================
Watches Kenyan procurement portals for RFPs matching GDC's service lines:
event management/design, AV & technical production, simultaneous
interpretation equipment (SIE), registration/ticketing, branding &
creative, PR & comms, data viz, consultancy.

WHY PLAYWRIGHT, NOT REQUESTS:
tenders.go.ke (PPIP) is a JavaScript single-page app — fetching the raw
HTML returns an empty shell with no tender data (I verified this: the
page source is just a <head> with no listings, because everything is
rendered client-side after the browser runs the JS bundle). A plain
`requests.get()` will never see the tenders. Playwright drives a real
(headless) browser, waits for the JS to run, then reads the rendered DOM.

STATUS: selectors below are now based on the real PPIP DOM (confirmed via
DevTools inspection of tenders.go.ke/tenders): a Vuetify `v-table` with a
plain <table><tbody><tr><td> structure — no per-row classes to hook into,
so we address cells by column position instead. Columns observed, in
order: Tender No, Description, Procuring Entity, Proc. Method,
Proc. Category, Closing Date, "Closes in N days", (a second date),
Status (e.g. "Addendum added").

The table also has a per-column search box (a "Search D..." box under
Description), but typing into it was found NOT to reliably trigger the
site's filtering (see scan_portal's docstring) — so keyword matching is
done in plain Python against every scraped row instead, across every
page of results (pagination is now handled automatically, see
go_to_next_page / try_increase_page_size).

ONE THING STILL UNVERIFIED: no <a href> was visible in the inspected
rows, so there's no confirmed way to build a direct link to each
tender's detail page from the table alone. The script leaves `link`
blank and notes the tender number instead; if PPIP opens details via a
click-triggered JS route rather than a real link, capturing that will
need a quick look at the Network tab when a row is clicked (or an
onclick page navigation captured via Playwright).

OUTPUT: each run generates a dated PDF in ./tender_reports/ with two
sections — "New Matches" (not seen in any previous run) and "Repeated"
(matched again this run but already logged before — still shown since a
deadline may be approaching). Requires `reportlab`.

GENERIC_SITES: alongside the structured PPIP table scan, this also
scans a set of UN/NGO/embassy/corporate/venue sites (see GENERIC_SITES
below) that aren't structured tender tables like PPIP. Rather than
whole-page text matching, it extracts every link on the page and
keyword-matches each one individually — a generalized, per-item
approach that works on any site's markup without hand-tuned selectors.
UNGM (UN Global Marketplace) and UNDP's own procurement-notices site
are REAL, VERIFIED URLs (confirmed via research) that together cover
UNDP/UNICEF/UNEP/WHO/UN-Habitat in one consolidated public portal — a
correction from an earlier draft that had five separate guessed and
partly-wrong agency URLs. The remaining entries (embassy, USAID,
corporate, venue, cultural-institute sites) are still best-guess URLs
that need manual verification in a browser — see the comments above
GENERIC_SITES for which is which.
"""

import sqlite3
import logging
import os
import re
from datetime import datetime
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# Anchor all file output to the folder this script lives in — not whatever
# directory the terminal happens to be in when you run it. Without this,
# running the script from "/" (root) fails because macOS won't let a normal
# user create files there.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 1. CONFIG — the only section you should need to touch
# ---------------------------------------------------------------------------

PORTALS = [
    {
        "name": "PPIP (tenders.go.ke)",
        "url": "https://tenders.go.ke/tenders",
        "row_selector": "table tbody tr",
        # Column positions, 1-indexed, from the observed table:
        # 1 Tender No | 2 Description | 3 Procuring Entity | 4 Proc. Method
        # 5 Proc. Category | 6 Closing Date | 7 "Closes in N days" | 8 date | 9 Status
        "tender_no_col": 1,
        "description_col": 2,
        "entity_col": 3,
        "deadline_col": 6,
        "closes_in_col": 7,
        # The search box under the Description column header — used to filter
        # server/client-side per keyword instead of paging through every tender.
        # Multiple fallbacks tried in order: case-insensitive placeholder match
        # first (the "i" flag handles "Search Description" vs "search description"
        # vs any casing), then a positional fallback (2nd input in the filter row,
        # matching the column order: Tender No, Description, Entity, ...).
        "description_search_selectors": [
            "input[placeholder*='Search D' i]",
            "input[placeholder*='description' i]",
            "thead input, .v-table thead input",  # generic: any input in the header row
        ],
        "description_search_position": 2,  # 2nd filter input = Description column
    },
    # Add more portals here once you've confirmed their structure, e.g.
    # county portals, UN/NGO vendor portals, corporate supplier portals.
]

# Card-grid portals: sites with a real, confirmed list of tender "cards"
# (title + date + description + link), structurally different from
# PPIP's <table> but just as extractable once the real DOM is confirmed.
# Amref below was confirmed via DevTools screenshot: a WordPress site
# with <ul class="tender-grid"><li class="tender-card">...</li></ul>.
CARD_GRID_PORTALS = [
    {
        "name": "Amref Health Africa",
        "url": "https://amref.org/tenders/",
        "card_selector": "ul.tender-grid li.tender-card",
        # Within each card: a heading for the title, a date somewhere in
        # the card, and a "Read the tender" link. No separate CSS class
        # was visible for date/description in the screenshot, so this
        # grabs the whole card's text for date detection instead of a
        # dedicated date selector — worth tightening if you find the
        # actual date element's class via a closer inspection.
        "title_selector": "h2, h3, h4",
        "link_selector": "a",
    },
]

# ---------------------------------------------------------------------------
# Generic sites — NOT structured tender tables like PPIP, just regular
# websites (UN agencies, embassies, corporate/venue sites). These don't
# have a PPIP-style table, so instead of column-position extraction, the
# scanner pulls every LINK on the page (text + href) and keyword-matches
# each one individually — closer to per-tender extraction than a single
# whole-page check, and it works on any site's markup without needing
# hand-tuned CSS selectors per site.
#
# VERIFIED via research (real, confirmed URLs — not guesses):
#   - UNGM (United Nations Global Marketplace) is a single public portal
#     covering ~40 UN agencies' procurement notices, including UNDP,
#     UNICEF, UNEP, WHO, and UN-Habitat. This REPLACES five separate
#     guessed agency URLs from the original list — one real source
#     instead of five unverified ones. Filtered here to notices tagged
#     Kenya; agencyEnglishAbbreviation=<CODE> can narrow to one agency
#     (e.g. UNDP, UNICEF, UNEP, WHO, UNHABITAT) if you want them split
#     into separate rows later.
#   - UNDP also has its own procurement notices site as a secondary
#     source (procurement-notices.undp.org) in case UNGM's Kenya filter
#     misses something UNDP-specific.
#
# STILL UNVERIFIED (best-guess URLs) — per your check, most of these
# came back dead (404s / wrong pages). Removed from the active list
# below: Amref (now confirmed real, moved to CARD_GRID_PORTALS above),
# and the venue/cultural-institute guesses (KICC, Sarit, Goethe, British
# Council) that are unlikely to be real procurement pages in the first
# place. Kept as a short list of remaining candidates worth a quick
# manual check before re-adding — swap in the confirmed URL once you've
# looked, or drop entirely if also dead:
#   US Embassy Kenya   — https://ke.usembassy.gov/business/
#   USAID              — https://www.usaid.gov/work-usaid/get-grant-or-contract
#   CARE International — https://www.care.org/about-us/vendors-and-suppliers/
#   EABL               — https://www.eabl.com/en/suppliers
#   Safaricom          — https://www.safaricom.co.ke/about/procurement
GENERIC_SITES = [
    {"name": "UNGM (UN Global Marketplace)",
     "url": "https://www.ungm.org/Public/Notice?countryCode=KE"},
    {"name": "UNDP Procurement Notices",
     "url": "https://procurement-notices.undp.org/search.cfm"},
    {"name": "eGP Kenya",            "url": "https://egpkenya.go.ke"},
    {"name": "ReliefWeb (Kenya)",    "url": "https://reliefweb.int/updates?search=Kenya"},
    {"name": "Devex Jobs & Tenders", "url": "https://www.devex.com/jobs"},
]

# Keywords pulled directly from GDC's service lines (company profile):
# events, event mgmt & tech support, SIE, creative/branding/digital,
# photo/video/livestreaming, PR & comms, data viz, consultancy.
#
# Split into two tiers, because a flat list produced heavy noise in testing:
# generic government tenders like "REGISTRATION OF SUPPLIERS FOR FY 2026-2027"
# or "CONSULTANCY SERVICES FOR ACTUARIAL RISK ASSESSMENT" matched purely
# because they contained the word "registration" or "consultancy" — words
# GDC's own services legitimately use, but which are also extremely common
# in totally unrelated procurement (supplier onboarding, engineering
# consultancy, land registration, etc).
#
# STRONG_KEYWORDS: specific enough on their own — a title containing any of
# these is very likely genuinely relevant regardless of other context.
STRONG_KEYWORDS = [
    "event management", "event design", "stage design", "audio visual",
    "av production", "livestream", "live stream", "streaming",
    "simultaneous interpretation", "interpretation equipment",
    "interpreter booth", "sound system", "pa system", "led screen",
    "hospitality services", "videography", "corporate identity",
]

# GENERIC_KEYWORDS: real GDC service terms, but too common in unrelated
# tenders to trust alone. Only counted as a match if the SAME title also
# contains one of EVENT_CONTEXT_WORDS below.
GENERIC_KEYWORDS = [
    "consultancy", "registration", "branding", "graphic design",
    "website development", "photography", "documentary",
    "public relations", "media relations", "communications strategy",
    "data visualization", "dashboard", "m&e", "monitoring and evaluation",
    "ticketing", "rsvp", "booth", "interpreter", "sie",
]

# Present alongside a GENERIC_KEYWORDS hit, these confirm it's actually
# about an event/conference/summit rather than generic government business.
EVENT_CONTEXT_WORDS = [
    "event", "conference", "summit", "forum", "exhibition", "expo",
    "workshop", "seminar", "gala", "convention", "symposium",
]

# "conference", "summit", "forum", "exhibition", "gala" are strong signals
# on their own too — a tender titled around one of these is virtually
# always event-related.
TARGET_KEYWORDS = STRONG_KEYWORDS + [
    "conference", "summit", "forum", "exhibition", "gala",
]

DB_PATH = os.path.join(SCRIPT_DIR, "gdc_tenders.db")
PDF_OUTPUT_DIR = os.path.join(SCRIPT_DIR, "tender_reports")


# ---------------------------------------------------------------------------
# 2. STORAGE — dedup so you're never alerted on the same tender twice
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
# 3. SCRAPING — Playwright renders the JS, then we read the DOM
# ---------------------------------------------------------------------------

def load_portal(page, portal):
    """Loads a portal and waits for the Vuetify table to hydrate."""
    page.goto(portal["url"], wait_until="networkidle", timeout=30000)
    page.wait_for_selector(portal["row_selector"], timeout=15000)
    page.wait_for_timeout(1000)  # small extra buffer for client-side render


def try_increase_page_size(page):
    """Vuetify data tables often have a 'Rows per page' dropdown at the
    bottom (commonly labeled with a select showing 10/25/50/100/All).
    If found, switch it to the largest option so fewer page-clicks are
    needed. Safe no-op if no such control exists."""
    try:
        select = page.query_selector("select")
        if not select:
            return
        options = select.query_selector_all("option")
        if not options:
            return
        # Pick the option with the largest numeric value, or "All" if present
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
            logging.info("  Increased rows-per-page to reduce pagination clicks")
    except Exception as e:
        logging.info("  Could not adjust rows-per-page (non-fatal): %s", e)


def go_to_next_page(page):
    """Looks for a 'next page' control (Vuetify tables typically use an
    icon button, often with aria-label containing 'next'). Clicks it if
    present and not disabled. Returns True if it navigated, False if there
    was no next page (i.e. we're on the last page)."""
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
    """Reads every currently-rendered row, regardless of any portal filter."""
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
            # No confirmed href in the table — see note in module docstring.
            "link": f"{portal['url']} — search Tender No: {tender_no}",
        })
    return tenders


def find_search_box(page, portal):
    """Currently unused by scan_portal (see its docstring for why) — kept
    here in case you want to re-enable search-box filtering later after
    confirming, in DevTools, that typing into it actually re-renders the
    table (e.g. watch the Elements panel while typing manually)."""
    for sel in portal.get("description_search_selectors", []):
        box = page.query_selector(sel)
        if box:
            return box

    pos = portal.get("description_search_position")
    if pos:
        all_inputs = page.query_selector_all("table input, thead input")
        if len(all_inputs) >= pos:
            return all_inputs[pos - 1]

    return None


def scan_portal(page, portal, max_pages=50):
    """Loads the portal, walks through every page of results (clicking
    'next' until it's disabled or max_pages is hit), and keyword-matches
    every tender's description against TARGET_KEYWORDS in Python.

    NOTE: earlier versions tried to use the portal's own search box to
    pre-filter server/client-side per keyword, typing each keyword in
    turn. In testing that produced false positives — typing into the box
    didn't reliably trigger the site's Vue/Vuetify reactivity, so every
    "filtered" view silently returned the full unfiltered table, and every
    tender (boreholes, police stations, classrooms — clearly unrelated to
    GDC's services) got tagged as matching every keyword. Rather than
    depend on a third-party site's JS behaving a certain way, we pull the
    full visible list on each page and do the matching ourselves — slower
    for very large tender counts, but correct regardless of how the
    portal's frontend behaves.

    max_pages is a hard safety cap so a broken 'next' button (e.g. one
    that never reports itself as disabled) can't loop forever.

    Matching is two-tier (see TARGET_KEYWORDS / GENERIC_KEYWORDS /
    EVENT_CONTEXT_WORDS above): STRONG_KEYWORDS/TARGET_KEYWORDS match on
    their own, but GENERIC_KEYWORDS (consultancy, registration, branding,
    etc — real GDC services that are also extremely common in unrelated
    government tenders) only count if the same title also contains an
    event-context word. This was added after testing showed the flat
    keyword list flagging things like generic "supplier registration"
    or "actuarial consultancy" tenders as if they were event-related.
    """
    load_portal(page, portal)
    try_increase_page_size(page)

    all_tenders = []
    seen_ids_this_scan = set()
    page_num = 1

    while page_num <= max_pages:
        page_tenders = extract_all_tenders(page, portal)
        new_on_this_page = 0
        for tender in page_tenders:
            if tender["id"] not in seen_ids_this_scan:
                seen_ids_this_scan.add(tender["id"])
                all_tenders.append(tender)
                new_on_this_page += 1

        logging.info("  Page %d: %d tenders (%d new)", page_num, len(page_tenders), new_on_this_page)

        # If a page brings zero new tenders, we've likely looped back or
        # stalled — stop rather than spin.
        if new_on_this_page == 0 and page_num > 1:
            break

        if not go_to_next_page(page):
            break
        page_num += 1

    logging.info("  %d total tenders visible across %d page(s)", len(all_tenders), page_num)

    matches = {}
    for tender in all_tenders:
        hits = match_keywords(tender["title"])
        if hits:
            tender["matched_keywords"] = ", ".join(hits)
            matches[tender["id"]] = tender

    return list(matches.values())


def match_keywords(text):
    """Shared two-tier keyword matcher, used by both the PPIP table scan
    and the generic-site page-text scan. See TARGET_KEYWORDS /
    GENERIC_KEYWORDS / EVENT_CONTEXT_WORDS above for the rationale."""
    text_lower = text.lower()
    strong_hits = [kw for kw in TARGET_KEYWORDS if kw in text_lower]
    generic_hits = [kw for kw in GENERIC_KEYWORDS if kw in text_lower]
    has_event_context = any(ctx in text_lower for ctx in EVENT_CONTEXT_WORDS)
    confirmed_generic_hits = generic_hits if has_event_context else []
    return strong_hits + confirmed_generic_hits


def scan_generic_site(page, site, max_items=100):
    """Loads a non-tender-table site and extracts every link on the page
    (text + href), keyword-matching each one individually. This is a
    generalized version of PPIP's row extraction: instead of hand-tuned
    column selectors (which only work because we inspected PPIP's exact
    DOM), this grabs every <a> tag's visible text and checks it against
    the same two-tier keyword logic — works on any site's markup without
    per-site customization, at the cost of being less structured (no
    separate deadline/entity columns, since generic sites don't have
    them in a consistent place the way PPIP's table does).

    Returns a list of hit dicts (0, 1, or many per site) — much closer
    to PPIP's per-tender extraction than the old whole-page check.

    CAVEAT: still only checks the single URL configured in GENERIC_SITES,
    not the whole site — if pagination or a "load more" button hides
    further notices, those won't be seen. And if a site's real listing
    lives on a different page than what's configured, this misses it
    entirely — worth spot-checking each URL in a browser first.
    """
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
        if not text or len(text) < 8:  # skip nav labels, icons, etc.
            continue
        checked += 1

        matched = match_keywords(text)
        if not matched:
            continue

        href = link.get_attribute("href") or ""
        if href.startswith("/"):
            href = base + href
        elif not href.startswith("http"):
            continue  # skip mailto:, javascript:, anchors, etc.

        hit_id = href or f"{site['url']}#{text[:80]}"
        if hit_id in hits:
            continue  # same link matched via multiple keyword passes

        hits[hit_id] = {
            "id": hit_id,
            "portal": site["name"],
            "title": text,
            "entity": "",
            "deadline": "",
            "link": href or site["url"],
            "matched_keywords": ", ".join(matched),
        }

    logging.info("  %d links checked, %d matched", checked, len(hits))
    return list(hits.values())


def scan_card_grid_portal(page, portal, max_items=100):
    """For sites with a real, confirmed card-grid structure (a repeated
    container of title+date+description+link — e.g. Amref's
    <ul class="tender-grid"><li class="tender-card">...). Unlike PPIP's
    column-index table extraction, this pulls the whole card's text
    (title via title_selector, everything else via a full-text scan for
    dates) since these sites don't have PPIP's clean column structure.
    """
    try:
        page.goto(portal["url"], wait_until="networkidle", timeout=30000)
        page.wait_for_selector(portal["card_selector"], timeout=15000)
        page.wait_for_timeout(1000)
    except Exception as e:
        logging.warning("  Could not load %s: %s", portal["name"], e)
        return []

    cards = page.query_selector_all(portal["card_selector"])
    logging.info("  %d cards found", len(cards))

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

        # Rough date extraction: look for a "DD MON YYYY"-style pattern
        # anywhere in the card's text, since Amref's date isn't in its
        # own dedicated element/class based on the inspected screenshot.
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
# 4. ALERTING — swap this for a real Telegram/Slack webhook when ready
# ---------------------------------------------------------------------------

def send_alert(tender):
    logging.info(
        "🚨 NEW RFP [%s] %s | Deadline: %s | Matched: %s | %s",
        tender["portal"], tender["title"], tender["deadline"],
        tender["matched_keywords"], tender["link"],
    )
    # Example Telegram integration (uncomment and fill in once you have a bot token/chat id):
    #
    # import requests
    # BOT_TOKEN = "YOUR_BOT_TOKEN"
    # CHAT_ID = "YOUR_CHAT_ID"
    # msg = f"🚨 {tender['title']}\nDeadline: {tender['deadline']}\n{tender['link']}"
    # requests.post(
    #     f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
    #     data={"chat_id": CHAT_ID, "text": msg},
    # )


# ---------------------------------------------------------------------------
# 5. PDF REPORT — one PDF per run, listing only newly-found matches
# ---------------------------------------------------------------------------

def _urgency_color(deadline_text, colors):
    """Returns a reportlab color based on how soon a tender closes, so
    the deadline column visually flags what needs action first."""
    if not deadline_text:
        return colors.black
    text = deadline_text.lower()
    match = re.search(r"closes in (\d+)", text)
    if "today" in text or "tomorrow" in text:
        return colors.HexColor("#C8102E")  # red — urgent
    if match:
        days = int(match.group(1))
        if days <= 3:
            return colors.HexColor("#C8102E")  # red
        if days <= 7:
            return colors.HexColor("#E07B00")  # orange
    return colors.black


def _build_tender_table(tenders, header_bg_hex, cell_style, header_style, styles):
    """Builds one reportlab Table for a list of tenders (used for both
    the 'new' and 'repeated' sections so they look consistent)."""
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

    col_widths = [0.9*inch, 1.9*inch, 1.1*inch, 1.0*inch, 1.3*inch, 1.3*inch]
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
    """Builds a dated PDF with two sections: newly-found matches (fresh
    alerts this run) and, below them, matches seen in a previous run too
    ("Repeated") — still useful to see again since a deadline may be
    approaching. Returns the file path, or None if there's nothing to
    report in either section."""
    repeated_tenders = repeated_tenders or []

    if not new_tenders and not repeated_tenders:
        logging.info("No tenders (new or repeated) this run — skipping PDF.")
        return None

    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filepath = os.path.join(PDF_OUTPUT_DIR, f"gdc_tender_alerts_{stamp}.pdf")

    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle(
        "cell", parent=styles["Normal"], fontSize=8, leading=10
    )
    header_style = ParagraphStyle(
        "cellHeader", parent=styles["Normal"], fontSize=8, leading=10,
        textColor=colors.white, fontName="Helvetica-Bold",
    )
    title_style = ParagraphStyle(
        "gdcTitle", parent=styles["Title"], textColor=colors.HexColor("#C8102E"),
    )
    section_new_style = ParagraphStyle(
        "sectionNew", parent=styles["Heading2"], textColor=colors.HexColor("#C8102E"),
    )
    section_repeat_style = ParagraphStyle(
        "sectionRepeat", parent=styles["Heading2"], textColor=colors.HexColor("#777777"),
    )
    subtitle_style = ParagraphStyle(
        "gdcSubtitle", parent=styles["Normal"], textColor=colors.HexColor("#555555"),
    )

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
        story.append(_build_tender_table(new_tenders, "#C8102E", cell_style, header_style, styles))  # GDC red
        story.append(Spacer(1, 20))

    if repeated_tenders:
        story.append(Paragraph("Repeated (seen in a previous run)", section_repeat_style))
        story.append(Spacer(1, 6))
        story.append(_build_tender_table(repeated_tenders, "#777777", cell_style, header_style, styles))  # grey

    doc.build(story)
    logging.info("PDF report written to %s", filepath)
    return filepath


# ---------------------------------------------------------------------------
# 6. MAIN RUN
# ---------------------------------------------------------------------------

def process_hits(hits, cursor, conn, new_list, repeated_list):
    """Shared dedup + insert logic used across all three scan types
    (PPIP table, card-grid portals, generic link scan) so run() doesn't
    repeat the same six lines three times."""
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