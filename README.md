# ProcureFind

**Supplier search and price comparison for Angani procurement.**

ProcureFind lets Angani staff search for an item, see offers from Angani's trusted (Tier 1) suppliers, compare them on **total cost including VAT**, and draft a request for quotation (RFQ) when a supplier doesn't publish a price. Every price shown comes from a real supplier web page, with its source link and the time it was fetched.

| | |
| --- | --- |
| Status | **Demo build, no sign-in.** Phase 1 (Tier 1 live search, VAT, comparison, RFQs, audit trail) plus Phase 2 groundwork (Tier 2/3 suppliers from OpenStreetMap, ratings, PPRA check) and price history |
| Owner | Joseph |
| Full specification | `files/ProcureFind.md` (kept locally, not in the repository) |
| GPU server notes | `files/gpu-server-handoff.md` (kept locally, not in the repository) |

---

## Quick start

Needs Python 3.10 or newer.

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional: set DEMO_USER (see Settings)
./run.sh                      # http://127.0.0.1:8090
venv/bin/pytest               # run the tests
```

This is a **demo build: there is no sign-in page**. The app opens straight on the search dashboard, and actions are recorded under `DEMO_USER`. Don't expose it beyond a trusted network.

Out of the box it runs in **practice mode**: no AI server and no internet needed. Supplier prices come from saved pages in `tests/fixtures/` (a yellow bar says so on every page). To use the real supplier websites and the AI server, set in `.env`:

```bash
LLM_BASE_URL=http://192.168.95.6:11434   # gpu-01, office network only
SUPPLIER_SOURCE=live                     # the default when LLM_BASE_URL isn't "mock"
```

You can also use live supplier prices with practice-mode AI (`LLM_BASE_URL=mock` with `SUPPLIER_SOURCE=live`).

## Settings (`.env`)

`.env` is never committed. [.env.example](.env.example) lists every setting.

| Setting | What it does | Default |
| --- | --- | --- |
| `DEMO_USER` | Name recorded on searches, choices and RFQs (demo build has no sign-in) | `Demo` |
| `SECRET_KEY` | Signs the short-lived page messages | temporary random key |
| `LLM_BASE_URL` | Ollama server, or `mock` for practice mode | `mock` |
| `LLM_MODEL`, `LLM_TIMEOUT` | Model name and AI timeout in seconds | `qwen3:8b`, `300` |
| `SUPPLIER_SOURCE` | `live` (supplier websites) or `fixtures` (saved pages) | `fixtures` in practice mode, otherwise `live` |
| `CONTACT_EMAIL` | Optional contact shown to supplier sites in the User-Agent | empty (`ProcureFind/0.1 (Angani; +https://angani.co)`) |
| `VAT_RATE` | VAT added to "excl. VAT" prices | `0.16` |
| `CACHE_MINUTES` | Reuse a supplier's results for the same search for this long | `60` |
| `FETCH_TIMEOUT` | Seconds to wait for each supplier before showing "didn't respond" | `15` |
| `HOST`, `PORT` | Where `run.sh` serves the app | `127.0.0.1`, `8090` |
| `PROCUREFIND_DB` | SQLite database file | `procurefind.db` |
| `DEFAULT_LOCATION` | Where to look for Tier 2/3 suppliers when a search has no location | `Nairobi` |
| `DISCOVERY_DAYS` / `SITE_CHECK_DAYS` | How often to refresh the nearby-supplier directory / re-check a supplier's website | `7` / `7` |
| `MAX_NEW_SITE_CHECKS` | Unchecked supplier websites checked per search (keeps searches fast and polite) | `4` |
| `OVERPASS_URLS` | OpenStreetMap search servers, tried in order | overpass-api.de, then overpass.kumi.systems |

Suppliers are configured in [suppliers.yaml](suppliers.yaml): website, VAT basis (`excl` / `incl` / `unknown`), status (`active` / `rfq_only` / `blocked`) and the evidence for each. Edit it and restart the app.

## What it does

| Page | |
| --- | --- |
| **Search** (`/`, the dashboard) | One search bar (filters for location, price range incl. VAT, quantity and brand fold out). Recent searches appear as chips under the bar and as tiles below; they reopen instantly from history. Results **stream in supplier by supplier** as each site answers, under a summary banner (cheapest total incl. VAT, offers, same-product matches, which suppliers answered). Filter chips (all / in stock / with price / no public price / per supplier / same product), a text filter and a sort let you go through them quickly. Each offer shows the total incl. VAT (× quantity), the price exactly as the site shows it, stock, source link and fetch time |
| **Compare** (`/compare`) | Press **+ Compare** on any offer, from any search, to collect up to 5. A bar at the bottom and a badge on the Compare tab show how many are collected (kept in this browser). The Compare tab lists them and past comparisons |
| **Comparison** (`/compare/<id>`) | Offers side by side, with a summary (AI when connected, checked against the prices; a plain rule-based summary otherwise). "Choose this offer" with a reason, saved to the audit trail |
| **RFQ** (`/rfq`) | Request-for-quotation draft with copy buttons. Always asks the supplier to say whether prices include VAT |
| **Suppliers** (`/suppliers`) | Tier 1 suppliers and the nearby-supplier directory for any location, each with a rating out of 100 and every point explained; **Update now** (OpenStreetMap + PPRA); **Add a supplier** by website |
| **History** (`/history`) | Searches, decisions and RFQs; decisions export to CSV (`/history.csv`) |
| **Status** (`/health`) | App, database, AI server and supplier websites (`/health?format=json` for monitoring) |

How a search runs ([search.py](search.py)): all active suppliers are searched at the same time, and `/api/search` sends each supplier's results to the page as soon as that supplier answers (newline-delimited JSON), each with its own timeout, so a slow site never blocks the others. Results are cached per supplier and search for `CACHE_MINUTES`. Prices are normalised in [prices.py](prices.py), and the price range filter applies to the total incl. VAT (or the listed price when VAT is unknown, with a note).

**VAT rules:** "+VAT", "Excl. Tax" and "Excl VAT" prices get 16% added. "Incl. VAT" prices are used as shown. If the VAT basis is unknown, the total is left empty and flagged, never guessed. "Call for Quote" or a missing price shows **No public price** with an RFQ button.

**Units:** per-metre, box (e.g. "305m"), and pack ("pack of 100") prices are detected from names. Comparisons warn "units differ" instead of comparing unlike units. A length on its own ("Patch Cord 3m") is the product's length, not a quantity.

**Matching** ([matching.py](matching.py)): model numbers first (e.g. `NS-5ACL`, `CRS326-24G-2S+RM`; also "Loco M5" vs "locoM5"), then normalised names and fuzzy similarity (threshold 85). Different model numbers or different lengths are never merged. Labels: *Same product*, *Similar (check specs)*, *Different*.

## Tier 2 and 3 suppliers, ratings and price history

**Finding suppliers.** For the search location (or `DEFAULT_LOCATION`), ProcureFind looks up IT, computer, electronics and telecoms shops and offices on OpenStreetMap. The directory is built in the background and refreshed weekly. Procurement can also **add a supplier** by website on the Suppliers page. Each search checks a few more supplier websites (`MAX_NEW_SITE_CHECKS`), politely, and remembers the result for a week.

**Prices from Tier 2/3.** Where a supplier's website is a WooCommerce or Shopify shop priced in KES, its prices are searched live like Tier 1. Their VAT basis is unknown unless the price says so, so no total is shown (flagged, never guessed). Everyone else appears under **Nearby suppliers** with rating, distance, contacts and a **Draft RFQ** button.

**Rating (0 to 100, every point explained).**

| Part | Max | Checks |
| --- | --- | --- |
| Legitimacy | 40 | Website loads over HTTPS, is the same business, isn't parked; mapped location; street address; phone; email; Kenyan `.ke` address; opening hours |
| Price transparency and competitiveness | 35 | Publishes prices; states VAT; shows stock; cheapest or within 5% in same-product comparisons (from ProcureFind's own searches) |
| Reputation | 25 | Brand partnerships shown on its website; chosen in Angani decisions; website answers ProcureFind reliably |
| Red flags | deduct / block | **PPRA debarred list → blocked**; website down, parked or redirecting to another business; prices far below the market |

The score is out of the checks that could actually be made, so a new supplier isn't penalised for missing history; each rating says how much evidence it rests on. **Tier 2 (verified)** needs a working, genuine website, mapped business details (legitimacy ≥ 24) and no red flags; otherwise **Tier 3 (unverified)**. Tier 1 stays first whatever its score.

**Price history and checks.** Every price fetched is kept, so offers show **▲/▼ % since** the last different price for the same product page. An offer more than 40% below the median of the same product at 3+ suppliers is flagged **Unusually low**.

Data: © OpenStreetMap contributors (ODbL); PPRA debarred firms list (ppra.go.ke). Nominatim is used at most once per second and every lookup is cached.

## Tier 1 suppliers

Investigated 12 Nov 2026 (specification section 13.5) with a few polite requests per site.

| Supplier | How ProcureFind reads it | Price / VAT | Stock | Status |
| --- | --- | --- | --- | --- |
| **Network Options & Solutions**<br>network-options.co.ke | WooCommerce Store API (JSON): `/wp-json/wc/store/v1/products?search=…` | `6,000.00 KSh +VAT` → **excl**. API prices in cents | Yes | Yes Live search |
| **City Telecommunication Centre (CTC)**<br>ctcsolutions.co.ke | WooCommerce Store API (as above) | `KSh8,000 Excl. Tax` → **excl**. API prices in whole shillings; SKUs often empty | Yes | Yes Live search. SiteGround bot protection sometimes refuses (HTTP 403): shown as "didn't respond", never worked around |
| **Optace Networks**<br>optacenetworks.com | Custom Next.js site. Every page embeds the full product list (~515 items); ProcureFind fetches it once per cache period and searches it locally, as the site's own search box does | `8,900 KES Excl VAT` on product pages; cart adds "VAT (16%)" → **excl**. Price `0` = "Call for Quote" | Not shown | Yes Live search |
| **Nile Computers**<br>nilecomputers.co.ke | - | - | - | Not available: website down (hosting account suspended): **RFQ only** |
| **OM IT**<br>om.ke | - | - | - | Not available: website broken (WordPress "critical error", HTTP 500; no products listed): **RFQ only** |

**Answers to open questions (spec section 14), pending business confirmation:** CTC Solutions is City Telecommunication Centre Ltd. Optace prices exclude VAT. Nile Computers and OM IT can't be assessed while their websites are offline. The Status page shows when their sites come back.

**Search behaviour worth knowing:** the WooCommerce shops match the search text as one phrase ("ubiquiti nanostation" finds nothing because the titles say "Ubiquiti airMAX NanoStation"). The reader therefore tries, in order: the search as typed, without lengths, the model-like word, and the longest word. It stops at the first search whose results pass ProcureFind's own relevance check, with at most 4 requests per supplier.

## Updating a supplier reader when a site changes

Each supplier has its own small reader in [adapters/](adapters/), with saved pages in `tests/fixtures/<supplier>/`. When a site changes, only that supplier's reader and saved pages need updating.

1. **Notice it.** Results show "*Supplier* couldn't be read: the site page layout changed…" or the supplier suddenly returns nothing. `/health` shows whether the site is reachable.
2. **Look at the site** in a browser: search for a known product and open its page. Check the price format, VAT wording, stock label and product link format.
3. **Save fresh pages** for offline tests, using the ProcureFind User-Agent. Make one or two requests, not a crawl:
   ```bash
   UA="ProcureFind/0.1 (Angani; +https://angani.co)"
   curl -A "$UA" "https://network-options.co.ke/wp-json/wc/store/v1/products?search=nanostation&per_page=20" \
        -o tests/fixtures/network_options/search-nanostation.json
   curl -A "$UA" https://optacenetworks.com/ -o tests/fixtures/optace/catalogue.html
   ```
4. **Update the reader:**
   - WooCommerce shops (`network_options.py`, `ctc_solutions.py`) share [adapters/woocommerce.py](adapters/woocommerce.py). Usually only `search_url` in `suppliers.yaml` or the price fields need changing.
   - Optace: [adapters/optace.py](adapters/optace.py) reads the embedded product list (`parse_catalogue`).
   - A new layout: write a new `adapters/<supplier>.py` with a class that has `async def search(self, query, fetcher) -> list[Offer]`. Register it in [adapters/\_\_init\_\_.py](adapters/__init__.py) and set `adapter:` in `suppliers.yaml`.
   - Readers return **what the page says** (`price_text` exactly as shown). Price parsing, VAT and units are done in `prices.py`.
5. **Update the expected names and prices** in [tests/test_adapters.py](tests/test_adapters.py) and run `venv/bin/pytest`.
6. If a site goes down or starts refusing automated requests, set `status: rfq_only` (or `blocked`) in `suppliers.yaml` with a note. Don't work around it.

## Project layout

```
app.py              FastAPI app: dashboard, streaming search API, compare/choose, RFQ, history, CSV, health
config.py           Settings from .env; loads suppliers.yaml
suppliers.yaml      Tier 1 suppliers: website, VAT basis, status, evidence
search.py           Runs a search across suppliers, cache, filters, grouping, price history, nearby list
adapters/           One reader per supplier (+ shared WooCommerce reader, polite fetching, generic.py for Tier 2/3 shops)
discover.py         Tier 2/3 suppliers from OpenStreetMap (Nominatim + Overpass), background refresh, add-a-supplier
sitecheck.py        Checks a supplier's website: loads? HTTPS? parked? same business? readable shop? brand partners
ratings.py          Supplier rating out of 100 with reasons; Tier 2/3 assignment; low-price flags
debarred.py         PPRA debarred firms list (red-flag check)
prices.py           Price text parsing, VAT and unit normalisation
matching.py         Model numbers, name normalisation, relevance, same-product matching
llm.py              Ollama client, fact-checked summaries, RFQ wording, practice mode
rfq.py              RFQ template
db.py               SQLite tables and migrations
fixture_source.py   Practice mode: serves saved supplier pages instead of the internet
templates/, static/ Pages, styles (light and dark mode) and app.js (streaming results, filters, compare basket)
tests/              pytest suite and saved supplier pages (fixtures)
prototype/          Earlier static UI prototype (fictional data), kept for reference
```

The static prototype still runs on its own: `cd prototype && python3 -m http.server 3000`. All its data is fictional.

## Phase 1 limitations

- **No sign-in (demo build).** Everyone shares one name (`DEMO_USER`) in the audit trail. Add sign-in back before real use.
- **Tier 2/3 prices** are read only from WooCommerce and Shopify shops priced in KES. Most suppliers on OpenStreetMap have no website or a broken one (in Nairobi on 12 Nov 2026: 104 suppliers, 10 with a website, none with a readable shop), so they appear as *nearby suppliers* with ratings and contacts for RFQs. The generic HTML/AI page reader for other shop types is still to do.
- **First directory load is slow.** OpenStreetMap's search servers can take minutes, so it runs in the background and is cached for a week; a search before it's ready shows Tier 1 only, with a note.
- **Ratings** have no public-review part yet (no review source with terms that allow it). Price competitiveness and reliability build up as ProcureFind is used; until then they're marked "n/a" and don't count for or against a supplier.
- **AI match checks** for borderline names are not built yet. Matching uses model numbers and fuzzy names only.
- **Location** is informational for Tier 1: all three live suppliers are in Nairobi, and a note asks you to confirm delivery elsewhere.
- **AI request parsing** only runs for long, sentence-like searches when the AI server is connected. The structured form fields are the main input.
- **Pasting RFQ replies back** into a comparison is not built yet.

## Rules ProcureFind follows

- **No invented data.** Suppliers, products and prices always come from a real web page. The AI only summarises and drafts. Every number in an AI summary is checked against the actual prices; if any doesn't match, the plain summary is shown instead.
- **VAT is never guessed.** An unknown VAT basis is flagged and no total is shown.
- **Polite fetching.** ProcureFind:
  - fetches only when someone searches, with a cache;
  - makes at most one request per second per site;
  - sends a clear `ProcureFind` User-Agent;
  - respects `robots.txt`.

  A refusal (403, CAPTCHA, login) is reported as "didn't respond" and never worked around. ProcureFind never crawls whole sites.
- **Secrets stay out of the repository.** `.env`, databases and local tool settings are listed in [.gitignore](.gitignore).
