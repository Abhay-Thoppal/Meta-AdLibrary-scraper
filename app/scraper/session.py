"""§10.1 — Playwright bootstraps a logged-out session (cookies + form tokens + doc_ids); §10.3 browser fallback."""
import asyncio
import json
import logging
import random
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs

from playwright.async_api import async_playwright

from app.config import settings

log = logging.getLogger(__name__)
DOC_IDS_FILE = Path(__file__).resolve().parents[2] / "doc_ids.json"
BOOT_URL = ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL"
            "&media_type=all&search_type=keyword_unordered&q=shoes")
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
]
TOKENS = {  # form field -> regex over the page HTML
    "lsd": r'"LSD",\[\],\{"token":"([^"]+)"',
    "__hs": r'"haste_session":"([^"]+)"',
    "__rev": r'"server_revision":(\d+)',
    "__hsi": r'"hsi":"(\d+)"',
    "__comet_req": r'"comet_env":(\d+)',
    "__spin_r": r'"__spin_r":(\d+)',
    "__spin_b": r'"__spin_b":"([^"]+)"',
    "__spin_t": r'"__spin_t":(\d+)',
}


REPAIR_COOLDOWN = 3600  # seconds between doc_id re-captures, so a Meta outage can't make us scrape in a loop


async def dismiss_cookies(page):
    for name in ("Allow all cookies", "Decline optional cookies"):
        btn = page.get_by_role("button", name=name)
        if await btn.count():
            await btn.first.click()
            await page.wait_for_timeout(1000)
            return


async def type_in_search(page, text):
    """Typing in the advertiser box fires useAdLibraryTypeaheadSuggestionDataSourceQuery."""
    box = page.locator("input[type=search]").first
    if await box.count():
        await box.click()
        await box.fill("")
        await box.press_sequentially(text, delay=150)
        await page.wait_for_timeout(4000)


async def click_ad_details(page):
    """On an ad's ?id= page, 'See ad details' fires AdLibraryV3AdDetailsQuery (a modal overlays it: force-click)."""
    for label in ("See ad details", "See summary details"):
        btn = page.get_by_text(label, exact=True)
        if await btn.count():
            try:
                await btn.last.click(force=True, timeout=5000)
            except Exception as e:
                log.warning("details click failed: %s", str(e)[:120])
            await page.wait_for_timeout(4000)
            return


class Session:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.repair_lock = asyncio.Lock()
        self.last_repair = 0.0
        self.pw = self.browser = self.page = None
        self.cookies, self.form, self.ua, self.created = {}, {}, UAS[0], 0.0
        self.session_id = str(uuid.uuid4())
        self.doc_ids = json.loads(DOC_IDS_FILE.read_text()) if DOC_IDS_FILE.exists() else {}

    @property
    def age(self):
        return time.time() - self.created if self.created else None

    async def ensure(self, force=False):
        if not force and self.created and self.age < settings.session_ttl_minutes * 60:
            return
        async with self.lock:
            if not force and self.created and self.age < settings.session_ttl_minutes * 60:
                return
            await self._bootstrap()

    async def _bootstrap(self):
        await self.close()
        log.info("bootstrapping Meta session")
        self.pw = await async_playwright().start()
        proxy = {"server": settings.proxy_url} if settings.proxy_url else None
        self.browser = await self.pw.chromium.launch(headless=settings.headless, proxy=proxy)
        self.ua = random.choice(UAS)
        ctx = await self.browser.new_context(user_agent=self.ua, locale="en-US")
        self.page = await ctx.new_page()
        self.page.on("request", self._sniff_doc_id)
        await self.page.goto(BOOT_URL, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(4000)
        await dismiss_cookies(self.page)
        await self.page.mouse.wheel(0, 6000)  # fires a pagination query -> fresh doc_id
        await self.page.wait_for_timeout(2500)
        html = await self.page.content()
        form = {"av": "0", "__aaid": "0", "__user": "0", "__a": "1", "__req": "1", "dpr": "1", "__ccg": "EXCELLENT",
                "fb_api_caller_class": "RelayModern", "server_timestamps": "true"}
        for field, rx in TOKENS.items():
            if m := re.search(rx, html):
                form[field] = m.group(1)
        if "lsd" not in form:
            raise RuntimeError("could not extract lsd token from Ad Library page (blocked or layout changed)")
        form["jazoest"] = "2" + str(sum(map(ord, form["lsd"])))
        self.form = form
        self.cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
        self.created = time.time()
        log.info("session ready; doc_ids=%s", self.doc_ids)

    def _sniff_doc_id(self, req):
        if "/api/graphql" in req.url and req.method == "POST":
            f = parse_qs(req.post_data or "")
            if f.get("fb_api_req_friendly_name") and f.get("doc_id"):
                self.doc_ids[f["fb_api_req_friendly_name"][0]] = f["doc_id"][0]

    async def repair_doc_ids(self):
        """Re-capture doc_ids the way discover.py does, in a spare tab: search page (pagination), typing in the
        advertiser box (typeahead) and one ad's details panel (details). Saves doc_ids.json. Once per REPAIR_COOLDOWN."""
        async with self.repair_lock:
            if time.time() - self.last_repair < REPAIR_COOLDOWN:
                return
            self.last_repair = time.time()
            await self.ensure()
            before = dict(self.doc_ids)
            log.warning("re-capturing Meta doc_ids (a query looked stale)")
            page = await self.page.context.new_page()
            page.on("request", self._sniff_doc_id)
            try:
                await page.goto(BOOT_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(4000)
                await dismiss_cookies(page)
                await page.mouse.wheel(0, 6000)
                await page.wait_for_timeout(2500)
                ad_ids = re.findall(r'"ad_archive_id":"(\d+)"', await page.content())
                await type_in_search(page, "nike")
                if ad_ids:
                    await page.goto(f"https://www.facebook.com/ads/library/?id={ad_ids[0]}", wait_until="domcontentloaded")
                    await page.wait_for_timeout(4000)
                    await click_ad_details(page)
            except Exception as e:
                log.error("doc_id re-capture failed: %s", e)
            finally:
                await page.close()
            changed = {k: v for k, v in self.doc_ids.items() if before.get(k) != v}
            for k, v in changed.items():
                log.warning("doc_id refreshed: %s %s -> %s", k, before.get(k), v)
            if changed:
                DOC_IDS_FILE.write_text(json.dumps(self.doc_ids, indent=2))
            else:
                log.warning("doc_ids unchanged; the failure wasn't a stale doc_id")

    def headers(self, friendly_name=None):
        h = {"user-agent": self.ua, "accept-language": "en-US,en;q=0.9", "origin": "https://www.facebook.com",
             "referer": BOOT_URL, "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
             "x-fb-lsd": self.form.get("lsd", "")}
        if friendly_name:
            h["x-fb-friendly-name"] = friendly_name
        return h

    async def browser_fetch(self, url, form=None):
        """§10.3: run the same request from inside the real browser page (its cookies, TLS and headers)."""
        log.warning("BROWSER FALLBACK used for %s", form.get("fb_api_req_friendly_name") if form else url)
        await self.ensure()
        return await self.page.evaluate(
            """async ([url, form]) => {
                const opts = form ? {method: 'POST', body: new URLSearchParams(form), credentials: 'include',
                    headers: {'x-fb-lsd': form.lsd, 'content-type': 'application/x-www-form-urlencoded'}} : {};
                const r = await fetch(url, opts);
                return [r.status, await r.text()];
            }""",
            [url, form],
        )

    async def close(self):
        for thing in (self.browser, self.pw):
            if thing:
                try:
                    await (thing.close() if thing is self.browser else thing.stop())
                except Exception:
                    pass
        self.pw = self.browser = self.page = None


session = Session()
