"""
GDC OSINT / RFQ Monitor — Prototype
=====================================
Companion to gdc_tender_monitor.py. Where that script watches PPIP for
formal, already-published TENDERS, this one hunts for earlier or
off-portal signals: RFQs (private, sent to pre-approved vendors — rarely
public), RFPs, IFBs (Invitations for Bid), and "save the date" / early
event announcements that haven't turned into a formal tender yet.

METHOD: Google Programmable Search Engine (Custom Search JSON API).
Regular google.com scraping gets blocked almost immediately, so this
uses Google's official API instead — free for 100 queries/day, then
$5 per 1,000 beyond that.

SETUP (one-time, ~10 minutes):
  1. Go to https://programmablesearchengine.google.com and create a
     search engine.
  2. IMPORTANT (as of Jan 2026): Google discontinued "Search the entire
     web" for newly-created engines — new engines must specify up to 50
     explicit domains under "Sites to search" instead. So: add the
     organizations/portals you actually want watched (UN agencies,
     embassies, venues, PPIP, corporate sites, etc) there. This isn't a
     downgrade for our purposes — it means results are pre-scoped to
     sources you actually care about rather than the open web.
  3. Copy the Search Engine ID (labeled "cx") from the Overview page.
  4. Go to Google Cloud Console, enable the "Custom Search API", and
     create an API key.
  5. Fill in SEARCH_ENGINE_ID and API_KEY below (SEARCH_ENGINE_ID is
     already filled in from your control panel screenshot).

STATUS: this is a working shape, same honesty note as the tender
monitor — I cannot execute live queries against Google's API from this
environment (no outbound network access here), so the query logic and
GDC-relevant dork list are built and ready, but you'll want to run it
yourself and sanity-check the first batch of results before relying on
it. If a query returns zero results, that's much more likely a query
being too narrow than a bug — Google dorks are finicky and often need
a few rounds of tuning.

WHAT THIS DELIBERATELY DOES NOT DO:
Vendor-list / exhibitor-list dorks (harvesting other companies' contact
sheets) are left out on purpose. Watching for public event/tender
announcements is one thing; scraping other businesses' vendor rosters
on autopilot is a different thing ethically, so that's not automated
here — treat those dorks (if you use them at all) as occasional manual
lookups, not a scheduled job.
"""

import sqlite3
import logging
import os
import time
from datetime import datetime

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 1. CONFIG
# ---------------------------------------------------------------------------

# --- local secrets: read from secrets.env (kept off GitHub) or the environment ---
def _load_secrets(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets.env")):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_secrets()
SEARCH_ENGINE_ID = os.environ.get("SEARCH_ENGINE_ID", "YOUR_CX_ID_HERE")   # from programmablesearchengine.google.com
API_KEY = os.environ.get("GOOGLE_API_KEY", "YOUR_API_KEY_HERE")            # from Google Cloud Console

DB_PATH = os.path.join(SCRIPT_DIR, "gdc_osint.db")
PDF_OUTPUT_DIR = os.path.join(SCRIPT_DIR, "tender_reports")

# Starter set of queries. Grouped by what they're trying to catch.
#
# IMPORTANT: as of January 2026, Google discontinued "Search the entire
# web" for newly-created Programmable Search Engines — new engines must
# instead specify up to 50 explicit domains under "Sites to search" in
# the control panel (programmablesearchengine.google.com). That means
# this engine is now inherently domain-restricted, so `site:` dorks
# below are redundant (the engine already only searches your configured
# domains) — they're left as comments in case you later add a
# differently-scoped second engine. Keep the domain list in the control
# panel updated as you identify new organizations worth watching.
DORK_QUERIES = [
    # --- Formal solicitations that may not be on PPIP yet (RFP/RFQ/IFB) ---
    'allintitle:"request for proposal" OR "RFP" "event management" Kenya',
    'allintitle:"request for quotation" OR "RFQ" "event" Kenya',
    'intitle:"invitation for bid" OR "IFB" "event" OR "conference" Kenya',

    # --- Early signals: sponsorship decks, draft agendas, save-the-dates ---
    'filetype:pdf intitle:sponsorship "summit" Kenya 2026',
    'filetype:pdf intitle:sponsorship "conference" Kenya 2026',
    'ext:pdf inurl:draft "conference agenda" OR "event schedule" Kenya',
    'intitle:"save the date" "summit" OR "conference" Kenya 2026',

    # --- Kept generic (no site: needed — engine is already domain-scoped
    # to whatever you configured under "Sites to search") ---
    'filetype:pdf "save the date" AND "conference" Kenya',
    '"conference" OR "summit" OR "event" tender OR RFP OR RFQ',
]

# Simple relevance filter applied to result titles/snippets, same spirit
# as the tender monitor's keyword tiers — keeps obvious noise out.
RELEVANCE_KEYWORDS = [
    "event", "conference", "summit", "forum", "exhibition", "gala",
    "rfp", "rfq", "ifb", "invitation for bid", "sponsorship",
    "save the date", "expression of interest", "eoi",
]


# ---------------------------------------------------------------------------
# 2. STORAGE — SQLite, same dedup pattern as the tender monitor
# ---------------------------------------------------------------------------

def setup_database():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS osint_hits (
            id TEXT PRIMARY KEY,
            query TEXT,
            title TEXT,
            snippet TEXT,
            link TEXT,
            first_seen TEXT
        )
    ''')
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 3. SEARCH — Google Custom Search JSON API
# ---------------------------------------------------------------------------

def run_query(query, num_results=10):
    """Runs one query against the Custom Search API, returns raw result
    items (each with 'title', 'link', 'snippet')."""
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": API_KEY,
        "cx": SEARCH_ENGINE_ID,
        "q": query,
        "num": min(num_results, 10),  # API caps at 10 per request
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("items", [])
    except requests.RequestException as e:
        # Log the actual error body from Google — the generic exception
        # message alone doesn't say *why* it's a 403 (billing, wrong API
        # enabled, key restrictions, quota, invalid cx, etc). Only doing
        # this on the FIRST query to avoid spamming the same detail 9x.
        try:
            body = resp.json()
            reason = body.get("error", {}).get("message", "no message in response body")
            logging.error("Query failed [%s]: %s | Google says: %s", query, e, reason)
        except Exception:
            logging.error("Query failed [%s]: %s | (raw body: %s)", query, e, resp.text[:300])
        return []


def is_relevant(item):
    text = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
    return any(kw in text for kw in RELEVANCE_KEYWORDS)


def scan_all_queries():
    """Runs every configured dork query, filters for relevance, returns
    a flat list of hit dicts. One second's pause between queries to stay
    well under rate limits."""
    all_hits = []
    for query in DORK_QUERIES:
        logging.info("Running query: %s", query)
        items = run_query(query)
        logging.info("  %d raw results", len(items))

        for item in items:
            if not is_relevant(item):
                continue
            link = item.get("link", "")
            hit_id = link  # URL is a natural unique ID here
            all_hits.append({
                "id": hit_id,
                "query": query,
                "title": item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "link": link,
            })

        time.sleep(1)  # be polite / stay under rate limits

    return all_hits


# ---------------------------------------------------------------------------
# 4. PDF REPORT — reuses the same visual language as the tender monitor
# ---------------------------------------------------------------------------

def generate_pdf_report(new_hits, repeated_hits=None):
    repeated_hits = repeated_hits or []
    if not new_hits and not repeated_hits:
        logging.info("No OSINT hits (new or repeated) this run — skipping PDF.")
        return None

    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    os.makedirs(PDF_OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    filepath = os.path.join(PDF_OUTPUT_DIR, f"gdc_osint_alerts_{stamp}.pdf")

    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8, leading=10)
    header_style = ParagraphStyle(
        "cellHeader", parent=styles["Normal"], fontSize=8, leading=10,
        textColor=colors.white, fontName="Helvetica-Bold",
    )
    title_style = ParagraphStyle("gdcTitle", parent=styles["Title"], textColor=colors.HexColor("#C8102E"))
    subtitle_style = ParagraphStyle("gdcSubtitle", parent=styles["Normal"], textColor=colors.HexColor("#555555"))
    section_new_style = ParagraphStyle("sectionNew", parent=styles["Heading2"], textColor=colors.HexColor("#C8102E"))
    section_repeat_style = ParagraphStyle("sectionRepeat", parent=styles["Heading2"], textColor=colors.HexColor("#777777"))

    def build_table(hits, header_bg_hex):
        table_data = [[
            Paragraph("Matched Query", header_style),
            Paragraph("Title", header_style),
            Paragraph("Snippet", header_style),
            Paragraph("Link", header_style),
        ]]
        for h in hits:
            table_data.append([
                Paragraph(h.get("query", ""), cell_style),
                Paragraph(h.get("title", ""), cell_style),
                Paragraph(h.get("snippet", ""), cell_style),
                Paragraph(h.get("link", ""), cell_style),
            ])
        col_widths = [1.6*inch, 1.8*inch, 2.4*inch, 1.7*inch]
        tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_bg_hex)),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FBEAEC")]),
        ]))
        return tbl

    doc = SimpleDocTemplate(
        filepath, pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
    )
    story = [
        Paragraph("GDC OSINT / RFQ Alert Report", title_style),
        Paragraph(
            f"Generated {datetime.now().strftime('%d %b %Y, %H:%M')} — "
            f"{len(new_hits)} new, {len(repeated_hits)} repeated",
            subtitle_style,
        ),
        Spacer(1, 16),
    ]

    if new_hits:
        story += [Paragraph("New Hits", section_new_style), Spacer(1, 6),
                  build_table(new_hits, "#C8102E"), Spacer(1, 20)]
    if repeated_hits:
        story += [Paragraph("Repeated (seen in a previous run)", section_repeat_style),
                  Spacer(1, 6), build_table(repeated_hits, "#777777")]

    doc.build(story)
    logging.info("PDF report written to %s", filepath)
    return filepath


# ---------------------------------------------------------------------------
# 5. MAIN RUN
# ---------------------------------------------------------------------------

def run():
    if API_KEY == "YOUR_API_KEY_HERE" or SEARCH_ENGINE_ID == "YOUR_CX_ID_HERE":
        logging.error(
            "Set SEARCH_ENGINE_ID and API_KEY at the top of this script "
            "before running — see the module docstring for setup steps."
        )
        return

    conn = setup_database()
    cursor = conn.cursor()

    hits = scan_all_queries()
    logging.info("%d relevant hits across all queries this run", len(hits))

    new_hits, repeated_hits = [], []
    for hit in hits:
        cursor.execute("SELECT id FROM osint_hits WHERE id = ?", (hit["id"],))
        if cursor.fetchone():
            repeated_hits.append(hit)
            continue
        new_hits.append(hit)
        cursor.execute(
            "INSERT INTO osint_hits (id, query, title, snippet, link, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (hit["id"], hit["query"], hit["title"], hit["snippet"], hit["link"],
             datetime.now().isoformat()),
        )
        conn.commit()

    conn.close()
    generate_pdf_report(new_hits, repeated_hits)
    logging.info("Run complete.")


if __name__ == "__main__":
    run()