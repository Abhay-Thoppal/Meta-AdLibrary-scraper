# Meta Ad Library API

A self-hosted REST API for the public [Meta Ad Library](https://www.facebook.com/ads/library/). Search advertisers,
pull every ad a company is running, look up any single ad with its EU/UK transparency data, and transcribe video
ads. All of it comes back as clean JSON with computed stats. No Facebook account, no paid API, no external services:
one Python process, a headless browser and a SQLite file.

## Features

- **Advertiser search:** find Facebook pages by name, with likes, verification status, Instagram handle and followers.
- **Company ads by name or ID:** pass `companyName=Dettol` and the API finds the right page itself, preferring
  blue-verified brands over look-alike pages, and tells you what it picked (`resolved_page`). Or pass `pageId`.
- **Keyword search:** any-word or exact-phrase search across all advertisers, including political and issue ads, with
  the total match count.
- **Filters:** country, active/inactive status, media type (image, video, meme…), content language, delivery date
  window, and sorting by impressions or most recent.
- **Batch pagination:** ask for 1–100 ads per call with `numberOfRecords`. Batches are exact, and the returned
  `cursor` continues right after the last ad you received.
- **Ad details:** the full creative (text, images, videos, carousel cards, CTA, landing page) and, where Meta discloses
  it, EU/UK transparency data: total reach, age and gender breakdown per country, targeted locations, and payer and
  beneficiary.
- **Video transcripts:** downloads the ad's video and transcribes it locally with Whisper. The language is detected
  automatically, so mixed Hindi/English ads work too.
- **Per-ad stats:** ad type (image, video, carousel, dynamic creative, catalog…), how long it has been running,
  platforms, creative counts, landing domain, UTM detection, and spend, impression and reach ranges where Meta
  publishes them. Values come only from what Meta returns; nothing is estimated.
- **Per-batch summary:** active/inactive counts, ad types, platforms, days-running min/max/avg/median, distinct
  landing domains and creatives.
- **Slim mode:** `trim=true` returns just the core fields, stats and creative URLs.
- **Caching:** results are stored in SQLite. Ad details and transcripts can be served from cache with
  `cache_max_age`; identical list requests are cached briefly so you don't hammer Meta while exploring.
- **Built to survive Meta:** session bootstrap through a real browser, an automatic browser fallback when the fast
  path gets rate-limited, a shared throttle, and errors that tell you what actually happened. A block is never
  reported as "no ads" or "not found".
- **Self-repairing:** when Meta rotates its internal query ids, the server notices on the first failed call, re-captures
  them, saves `doc_ids.json` and retries, so there's no cron job to schedule. `scripts/discover.py` does the same manually.
- **Swagger UI** at `/docs` with every parameter, enum, default and example response. Optional API-key auth.

## Setup & run

```bash
uv sync                                 # add --extra transcribe for the transcript endpoint (faster-whisper)
uv run playwright install chromium
uv run uvicorn app.main:app --reload    # -> http://localhost:8000/docs
```

You need Python 3.12 (uv fetches it) and `ffmpeg` on PATH for transcripts. No uv on PATH? `pip install uv` and use `python -m uv …`.

Tests: `uv run pytest` (offline, uses `tests/fixtures/`). Live tests against Meta: `uv run pytest -m live`.

## How it works

1. **Startup:** headless Chromium opens the Ad Library once and collects a logged-out visitor session: cookies, the
   `lsd` security token and other form fields, and the current query ids (`doc_id`s). The session refreshes every
   `SESSION_TTL_MINUTES` or right after a failed call.
2. **Direct path (fast):** an HTTP/2 client (httpx) sends requests straight to Meta's `/api/graphql/` using that
   session. The browser isn't involved in each request.
3. **Browser path (fallback):** the same request runs from inside the real Ad Library page in that browser, so to Meta
   it looks like a normal visitor. It's used when the direct path keeps failing. A Meta rate limit (error `1675004`,
   which hits the direct path, not the real page) skips the retries and routes all calls through the browser for 10
   minutes, so we stop feeding the limiter. Both paths share the same `MAX_CONCURRENCY` cap and delay, and the log
   shows `BROWSER FALLBACK` when it's in use.
4. **Honest errors:** if Meta serves a page without ad data (a block or login wall), you get a 502, never an empty
   result or a false "not found". If both paths are limited, you get a 429. Back off and try later.
5. **Self-repair:** Meta's internal query ids (`doc_id`s) change when Meta edits a query, with no notice and no fixed
   schedule. When a query fails for any reason other than a rate limit, the server re-captures all ids once, the way
   a person would (search page, typing in the advertiser box, opening an ad's details), saves them to `doc_ids.json`
   and retries. Meta's explicit "GraphQL document with ID … was not found" skips retries and repairs immediately. The
   affected request takes about 25 s instead of failing, and the log shows `doc_id refreshed: <query> <old> -> <new>`.
   It re-captures at most once an hour, so an outage can't make it scrape in a loop. If ids didn't change, the original
   error is returned.
6. **Manual refresh:** `uv run python scripts/discover.py` (`--headful` to watch) does the same capture and also
   saves fresh traffic into `tests/fixtures/` for the offline tests.

| Meta source | Used for |
|---|---|
| `AdLibrarySearchPaginationQuery` | company ads, keyword search, every page incl. the first |
| `useAdLibraryTypeaheadSuggestionDataSourceQuery` | advertiser search, `companyName` resolution |
| `AdLibraryV3AdDetailsQuery` | EU/UK transparency, `aaa_info` on ad details |
| `?id=<ad>` page HTML (`deeplink_ad_archive`) | the ad itself on ad details |
| search page HTML | `searchResultsCount` (Meta only exposes the total there) |

## Environment

| Var | Default | |
|---|---|---|
| `API_KEY` | empty | If set, every `/v1` call needs header `x-api-key` (401 otherwise) |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Used by `uv run python -m app.main` |
| `PROXY_URL` | empty | http/socks proxy for both Chromium and httpx. Datacenter IPs get blocked; residential or home IPs work best |
| `HEADLESS` | `true` | `false` shows the bootstrap browser |
| `SESSION_TTL_MINUTES` | `30` | Session refresh interval |
| `MAX_CONCURRENCY` | `3` | Max parallel calls to Meta (there's also a 0.2–0.8 s random delay before each) |
| `LIST_CACHE_SECONDS` | `600` | Identical list requests are served from cache for this long |
| `CACHE_DB_PATH` | `./cache.sqlite3` | SQLite cache file |
| `WHISPER_MODEL` / `WHISPER_DEVICE` | `small` / `auto` | faster-whisper settings |
| `MAX_VIDEO_MB` | `100` | Download cap for transcription |
| `LOG_LEVEL` | `INFO` | |

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/v1/facebook/adLibrary/search/companies` | Find advertiser pages by name |
| GET, POST | `/v1/facebook/adLibrary/company/ads` | Ads run by one page (`pageId` or `companyName`) |
| GET, POST | `/v1/facebook/adLibrary/search/ads` | Keyword search across all advertisers |
| GET | `/v1/facebook/adLibrary/ad` | One ad in full, by `id` or Ad Library `url` |
| GET | `/v1/facebook/adLibrary/ad/transcript` | Transcript of a video ad |
| GET | `/health` | Session age and whether query ids are loaded |

POST takes the same fields as GET, as a JSON body (handy for long cursors).

Each success body starts with `{"success": true}`. Cache hits also
carry `"cached": true, "cached_at": "<ISO8601>"`. Errors look like `{"success": false, "error": "<code>", "message": "…"}`
with status 400 (bad params), 401 (API key), 404 (`company_not_found` / `ad_not_found`), 429 (Meta rate-limited us
after retries) or 502 (scrape failed, includes Meta's error text).

```bash
B=http://localhost:8000/v1/facebook/adLibrary

# Search for advertisers
curl "$B/search/companies?query=nike"

# Company ads by page id or by name (resolved_page shows what was picked: exact | alias | best_guess)
curl "$B/company/ads?pageId=15087023444&country=IN&status=ALL&media_type=VIDEO&sort_by=relevancy_monthly_grouped"
curl "$B/company/ads?companyName=Nike&country=IN"

# 30 at a time: pass the returned cursor back with the same numberOfRecords for the next 30
curl "$B/company/ads?companyName=Dettol&numberOfRecords=30"
curl -X POST "$B/company/ads" -H 'content-type: application/json' \
     -d '{"companyName":"Dettol","numberOfRecords":30,"cursor":"<cursor from previous response>"}'

# Keyword search
curl "$B/search/ads?query=running&search_type=keyword_exact_phrase&country=US&start_date=2026-01-01&end_date=2026-06-30"
curl -X POST "$B/search/ads" -H 'content-type: application/json' -d '{"query":"election","ad_type":"political_and_issue_ads","country":"US"}'

# Ad details
curl "$B/ad?url=https://www.facebook.com/ads/library?id=1870984290951787"
curl "$B/ad?id=1870984290951787&cache_max_age=1d&trim=true"

# Transcript (needs `uv sync --extra transcribe` + ffmpeg)
curl "$B/ad/transcript?id=1358011079653040&cache_max_age=7d"

curl http://localhost:8000/health
```

List parameters: `country` (ISO-2 or `ALL`), `status` (`ALL|ACTIVE|INACTIVE`, default `ACTIVE`), `media_type`
(`ALL|IMAGE|VIDEO|MEME|IMAGE_AND_MEME|NONE`), `language` (2-letter), `sort_by` (`total_impressions` |
`relevancy_monthly_grouped` = most recent), `start_date`/`end_date` (`YYYY-MM-DD`), `cursor`, `numberOfRecords`
(1–100, default 10), `trim`. Search also takes `search_type` and `ad_type`. Swagger at `/docs` lists all of them with
enums, defaults and examples.

### Response shapes

- **List endpoints** return ads with snake_case keys and string ids (`ad_archive_id`, `page_id`), plus `cursor`
  (`null` on the last batch) and `summary`. `company/ads` adds `resolved_page` when you searched by `companyName`.
- **Ad details** uses camelCase top-level keys with numeric ids (`adArchiveID`, `pageID`). There, `snapshot.body` is
  a plain string, `page_categories` is a dict, and `display_format` is lowercase. Inside list results those are an
  object, a list and uppercase respectively.
- **`stats`** on every ad: `ad_type` (`DPA > DCO > CAROUSEL > VIDEO > IMAGE > EVENT > TEXT`, else `UNKNOWN`),
  duration (`days_running` is inclusive, so a same-day ad = 1), platforms, creative counts, landing domain, UTM
  detection, page info and `delivery`.
- **`summary`** on list responses, computed over the returned batch.
- **`trim=true`**: envelope plus core ad fields, `stats` and a slim `snapshot`
  (`body, title, caption, cta_text, cta_type, link_url, display_format`, image/video URLs, slim cards).

## Known limits

- **`companyName`** only matches by name among blue-verified pages when any exist, because big brands have
  unverified look-alike pages with the exact name and no ads. Handles (`page_alias`) match any page. If
  `resolved_page` is wrong, take the right `page_id` from `search/companies` and pass `pageId`.
- **Spend, impressions and reach** are disclosed by Meta only for political/issue ads and EU/UK-delivered ads.
  For most commercial ads (e.g. India-only) `delivery.*` is `null`. That is expected, not a bug. Example: political
  `spend` comes as `$600K-$700K` → `spend_lower: 600000, spend_upper: 700000`. EU ads get `eu_total_reach`, age and
  gender from `aaa_info`.
- **Date filters mean "delivered within the window"** (Meta's semantics), so an ad that started earlier but was still
  running inside the window is included.
- **Meta serves 10 ads per page** no matter what page size we ask for, so `numberOfRecords=100` means about 10 Meta
  calls (5–15 s). Cursors starting with `pt.` carry an offset inside a Meta page; plain Meta cursors work too.
  Batches in multiples of 10 avoid re-fetching a page.
- **`searchResultsCount`** is only available on the first page (no `cursor`); later pages return `null`.
- **`media_type=VIDEO`** also returns DCO/DPA ads whose cards contain video, because that is how Meta filters.
- **Ad details:** `ad_creative_id`, `creation_time`, `body_translations`, `instagram_*` and `version` are `null` or
  empty, because Meta doesn't expose them to logged-out visitors. `snapshot.page_categories` is keyed by
  **category name**, because Meta doesn't give category ids here.
- **Transcripts:** silent videos return `transcript: null`. Meta exposes no captions, so audio is always run through
  Whisper.
- Meta's internal GraphQL changes without notice. Stale query ids repair themselves (see *How it works*), but if Meta
  changes the *shape* of its responses, the parsers need updating: run `discover.py` and compare the new fixtures.
- Scraping is against Meta's terms of service and can get IPs rate-limited or blocked. Keep concurrency low.
