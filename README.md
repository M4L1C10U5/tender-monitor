# Tender Monitor

Watches Kenyan procurement portals and targeted web searches for tenders that match a set of service
lines (event management, AV and technical production, interpretation equipment, registration/ticketing,
branding, PR, data visualisation, consultancy). It writes a dated PDF report of new and repeated matches.

**How it finds tenders**
1. Google Custom Search API: targeted queries against primary sites (embassies, museums, universities, PPIP)
2. Playwright scraper for the JavaScript tables on tenders.go.ke
3. Card-grid scraper for structured listings (e.g. Amref)
4. Generic link scraper for UN and other portal sites

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp secrets.env.example secrets.env   # add your Google API key and search engine ID
python monitor3.py
```

Reports go to `tender_reports/`. Seen tenders are tracked in a local SQLite database.

## Files
- `monitor3.py`: current version (search + scrapers + PDF report)
- `tender monitor.py`, `tender monitor2.py`, `osint monitor.py`: earlier versions

`secrets.env`, the databases and the reports stay on your computer and are never uploaded.
