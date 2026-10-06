"""Capture live Ad Library GraphQL traffic into tests/fixtures/ and refresh doc_ids.json.

uv run python scripts/discover.py [--headful]
"""
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.scraper.session import click_ad_details, dismiss_cookies, type_in_search  # noqa: E402  (shared with auto-repair)

FIX = ROOT / "tests" / "fixtures"
BASE = "https://www.facebook.com/ads/library/"
SEARCH = BASE + "?active_status=active&ad_type=all&country=ALL&media_type=all&search_type=keyword_unordered&q=running"
PAGE = BASE + "?active_status=active&ad_type=all&country=IN&media_type=all&search_type=page&view_all_page_id=15087023444"
EU = BASE + "?active_status=active&ad_type=all&country=FR&media_type=all&search_type=page&view_all_page_id=15087023444"


async def main(headful: bool):
    FIX.mkdir(parents=True, exist_ok=True)
    captured, doc_ids = [], {}

    async def on_response(resp):
        req = resp.request
        if "/api/graphql" not in req.url or req.method != "POST":
            return
        form = {k: v[0] for k, v in parse_qs(req.post_data or "").items()}
        name = form.get("fb_api_req_friendly_name", "unknown")
        try:
            body = await resp.text()
        except Exception:
            body = ""
        doc_ids[name] = form.get("doc_id")
        captured.append({"name": name, "doc_id": form.get("doc_id"), "variables": form.get("variables"),
                         "form": form, "headers": req.headers, "status": resp.status, "body": body})
        print(f"  graphql {name} doc_id={form.get('doc_id')} {len(body)}B")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headful)
        page = await browser.new_page(locale="en-US")
        page.on("response", on_response)

        async def visit(url, tag, scrolls=3):
            print(f"[{tag}] {url}")
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            await dismiss_cookies(page)
            if "?id=" in url:  # open the details dialog -> fires the ad-details query
                await click_ad_details(page)
            for _ in range(scrolls):
                await page.mouse.wheel(0, 6000)
                await page.wait_for_timeout(2500)
            html = await page.content()
            (FIX / f"{tag}.html").write_text(html, encoding="utf-8")
            return html

        html = await visit(SEARCH, "search_running")
        await visit(PAGE, "page_nike_in")

        # typeahead: type into the advertiser search box
        print("[typeahead]")
        await page.goto(SEARCH, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await type_in_search(page, "nike")

        # ad deep links: a real id from captured search payloads / html
        ids = re.findall(r'"ad_archive_id":"(\d+)"', html + "".join(c["body"] for c in captured))
        if ids:
            await visit(BASE + f"?id={ids[0]}", "ad_detail", scrolls=0)
        eu_html = await visit(EU, "page_nike_fr", scrolls=1)
        eu_ids = re.findall(r'"ad_archive_id":"(\d+)"', eu_html + "".join(c["body"] for c in captured[-5:]))
        if eu_ids:
            await visit(BASE + f"?id={eu_ids[0]}", "ad_detail_eu", scrolls=0)
        await browser.close()

    for i, c in enumerate(captured):
        (FIX / f"gql_{i:02d}_{c['name']}.json").write_text(json.dumps(c, indent=1), encoding="utf-8")
    known = {k: v for k, v in doc_ids.items() if v}
    path = ROOT / "doc_ids.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**old, **known}, indent=2))
    print(f"captured {len(captured)} graphql calls; doc_ids: {known}")


if __name__ == "__main__":
    asyncio.run(main("--headful" in sys.argv))
