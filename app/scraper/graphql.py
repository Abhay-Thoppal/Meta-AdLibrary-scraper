"""§10.2/§10.4 — direct GraphQL + HTML over httpx, with retry, politeness and browser fallback."""
import asyncio
import json
import logging
import random
import time

import httpx

from app.config import settings
from app.scraper.parsers import graphql_lines, meta_error
from app.scraper.session import session

log = logging.getLogger(__name__)
GRAPHQL = "https://www.facebook.com/api/graphql/"
RETRY_STATUS = {429, 500, 502, 503, 504}
_sem = asyncio.Semaphore(settings.max_concurrency)
_client: httpx.AsyncClient | None = None
RATE_LIMIT_COOLDOWN = 600  # seconds; Meta's direct-path limit cleared within ~10 min in testing
_browser_until = 0.0
STALE_DOC_ID = "GraphQL document with ID"  # Meta: "The GraphQL document with ID 123 was not found."


class MetaError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(http2=True, timeout=30, follow_redirects=True, proxy=settings.proxy_url or None)
    return _client


async def _polite(fn, url, form):
    """Every Meta call — direct or via the browser — shares one concurrency cap and a small random delay."""
    async with _sem:
        await asyncio.sleep(random.uniform(0.2, 0.8))
        return await fn(url, form)


async def _send(url, form):
    """One direct request with the current session. Returns (status, text)."""
    name = form.get("fb_api_req_friendly_name") if form else None
    h = session.headers(name)
    if form:
        r = await client().post(url, data=form, headers=h, cookies=session.cookies)
    else:
        h |= {"accept": "text/html,application/xhtml+xml", "sec-fetch-mode": "navigate",
              "sec-fetch-dest": "document", "sec-fetch-site": "none"}
        r = await client().get(url, headers=h, cookies=session.cookies)
    return r.status_code, r.text


def _check(status, text, form):
    """Raise MetaError on HTTP or Meta-level failure; return parsed JSON lines (GraphQL) or HTML text."""
    if status in RETRY_STATUS:
        raise MetaError(429 if status == 429 else 502, f"Meta HTTP {status}")
    if status >= 400:
        raise MetaError(502, f"Meta HTTP {status}")
    if not form:
        return text
    try:
        objs = graphql_lines(text)
    except Exception:
        raise MetaError(502, f"unparseable Meta response: {text[:200]}")
    if err := meta_error(objs):
        raise MetaError(429 if "rate" in err.lower() or "1675004" in err else 502, f"Meta error {err}")
    return objs


async def _fetch(url, form_fn=None):
    """Direct call; on failure refresh session and retry with backoff (max 3); then browser fallback.
    A rate limit skips straight to the browser (Meta throttles the direct path, not the real page) and keeps
    using it for RATE_LIMIT_COOLDOWN, so we stop feeding the limiter."""
    global _browser_until
    await session.ensure()
    last = MetaError(502, "Meta call failed")
    for attempt in range(3 if time.time() >= _browser_until else 0):
        form = form_fn() if form_fn else None
        try:
            return _check(*await _polite(_send, url, form), form)
        except (MetaError, httpx.HTTPError) as e:
            last = e if isinstance(e, MetaError) else MetaError(502, f"network error: {e}")
            log.warning("Meta call failed (attempt %d): %s", attempt + 1, last.message)
            if STALE_DOC_ID in last.message:
                raise last  # unambiguous: retries, re-bootstrap and the browser all hit the same wall -> gql() repairs
            if last.status == 429:
                _browser_until = time.time() + RATE_LIMIT_COOLDOWN
                log.warning("rate-limited: routing Meta calls through the browser for %ds", RATE_LIMIT_COOLDOWN)
                break  # a session refresh or more direct retries only deepen the limit
            if attempt == 0:
                await session.ensure(force=True)  # token / doc_id may be stale
            await asyncio.sleep(2 ** attempt + random.random())
    try:
        form = form_fn() if form_fn else None
        return _check(*await _polite(session.browser_fetch, url, form), form)
    except MetaError as e:
        last = e
    except Exception as e:
        log.error("browser fallback failed: %s", e)
    raise last


async def gql(name, variables):
    """POST a persisted query by friendly name. doc_id comes from the live session (refreshed on bootstrap).
    Auto-repair: a non-rate-limit failure may mean Meta rotated the doc_id, so re-capture once and retry if it moved."""
    def form():
        doc_id = session.doc_ids.get(name)
        if not doc_id:
            raise MetaError(502, f"no doc_id known for {name}; run scripts/discover.py")
        return session.form | {"fb_api_req_friendly_name": name, "doc_id": doc_id, "variables": json.dumps(variables)}
    try:
        return await _fetch(GRAPHQL, form)
    except MetaError as e:
        if e.status == 429:
            raise
        used = session.doc_ids.get(name)
        await session.repair_doc_ids()
        if session.doc_ids.get(name) == used:
            raise
        log.warning("retrying %s with refreshed doc_id %s", name, session.doc_ids.get(name))
        return await _fetch(GRAPHQL, form)


async def html(url):
    return await _fetch(url)
