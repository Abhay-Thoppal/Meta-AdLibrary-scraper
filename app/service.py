"""Orchestration: resolver (§7) -> Meta client -> parsers -> stats/summary -> cache (§9)."""
import asyncio
import base64
import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

import orjson

from app import cache
from app.config import settings
from app.models import CompanyAdsParams, SearchAdsParams
from app.scraper import graphql as meta
from app.scraper.parsers import find_key, html_find, parse_connection, parse_details, parse_typeahead
from app.scraper.session import session
from app.scraper.transcript import pick_video, transcribe_url
from app.stats import summary
from app.trim import trim_ad

log = logging.getLogger(__name__)
AGES = {"1d": 86400, "3d": 3 * 86400, "7d": 7 * 86400, "14d": 14 * 86400, "30d": 30 * 86400}
SORT = {"total_impressions": "SORT_BY_TOTAL_IMPRESSIONS",
        "relevancy_monthly_grouped": "SORT_BY_RELEVANCY_MONTHLY_GROUPED"}
LIBRARY = "https://www.facebook.com/ads/library/"


class ApiError(Exception):
    def __init__(self, status, error, message):
        super().__init__(message)
        self.status, self.error, self.message = status, error, message


def envelope(data, cached_at=None):
    out = {"success": True}
    if cached_at is not None:
        out |= {"cached": True, "cached_at": datetime.fromtimestamp(cached_at, UTC).isoformat()}
    return out | data


# ---------- companies / resolver ----------

async def typeahead(query):
    objs = await meta.gql("useAdLibraryTypeaheadSuggestionDataSourceQuery",
                          {"queryString": query, "isMobile": False, "country": "ALL", "adType": "ALL"})
    return parse_typeahead(objs)


async def search_companies(query):
    return envelope({"searchResults": await typeahead(query)})


def pick_page(name, results):
    """§7: exact name > exact alias > best guess (most likes).
    Names aren't unique: big brands have squatter pages with the exact name and zero ads ('Dettol' -> a 1-like page,
    not Dettol India), so names and best guess only consider verified pages when any exist. Handles are unique,
    so an alias match counts on any page."""
    if not results:
        return None
    n = name.strip().lstrip("@").lower()
    by_likes = lambda rows: sorted(rows, key=lambda r: r.get("likes") or 0, reverse=True)  # noqa: E731
    pool = by_likes([r for r in results if r.get("verification") == "BLUE_VERIFIED"] or results)
    hit, match = next((r for r in pool if (r.get("name") or "").lower() == n), None), "exact"
    if not hit:
        hit, match = next((r for r in by_likes(results) if (r.get("page_alias") or "").lower() == n), None), "alias"
    if not hit:
        hit, match = pool[0], "best_guess"
    return {"page_id": hit["page_id"], "name": hit["name"], "match": match, "candidates": len(results)}


async def resolve(name):
    k = cache.key("resolve", {"name": name.strip().lower()})
    if hit := await cache.get(k, AGES["7d"]):
        return hit[0]
    resolved = pick_page(name, await typeahead(name))
    if not resolved:
        raise ApiError(404, "company_not_found", f"No Ad Library page found for '{name}'")
    await cache.put(k, resolved)
    return resolved


# ---------- ad lists ----------

def search_vars(p, page_id=None, cursor=None):
    """Our params -> AdLibrarySearchPaginationQuery variables (names/casing verified in discovery)."""
    s, e = p.start_date, p.end_date
    return {
        "activeStatus": p.status.lower(), "adType": getattr(p, "ad_type", "all").upper(), "bylines": [],
        "collationToken": None, "contentLanguages": [p.language.lower()] if p.language else [],
        "countries": [p.country.upper()], "cursor": cursor, "excludedIDs": None, "first": 30,
        "isTargetedCountry": False, "location": None, "mediaType": p.media_type.lower(),
        "multiCountryFilterMode": None, "pageIDs": [], "potentialReachInput": None, "publisherPlatforms": [],
        "queryString": getattr(p, "query", "") or "", "regions": None,
        "searchType": getattr(p, "search_type", "page"), "sessionID": session.session_id,
        "sortData": {"direction": "DESCENDING", "mode": SORT[p.sort_by]}, "source": None,
        "startDate": {"min": s and s.isoformat(), "max": e and e.isoformat()} if s or e else None,
        "viewAllPageID": page_id or "0",
    }


def search_url(p):
    """Same search as a browser URL — page 1 HTML is the only place Meta exposes the total `count`."""
    q = {"active_status": p.status.lower(), "ad_type": p.ad_type, "country": p.country.upper(),
         "media_type": p.media_type.lower(), "search_type": p.search_type, "q": p.query,
         "sort_data[direction]": "desc", "sort_data[mode]": p.sort_by}
    if p.language:
        q["content_languages[0]"] = p.language.lower()
    if p.start_date:
        q["start_date[min]"] = p.start_date.isoformat()
    if p.end_date:
        q["start_date[max]"] = p.end_date.isoformat()
    return LIBRARY + "?" + urlencode(q)


async def _count(p):
    try:
        conn = html_find(await meta.html(search_url(p)), "search_results_connection")
        return (conn or {}).get("count")
    except Exception as e:  # count is nice-to-have; never fail the page for it
        log.warning("searchResultsCount unavailable: %s", e)
        return None


async def _page(p, page_id, cursor):
    """One Meta page (Meta caps it at 10 ads whatever `first` says) -> (ads, collation_ids, next_cursor)."""
    objs = await meta.gql("AdLibrarySearchPaginationQuery", search_vars(p, page_id, cursor))
    conn = next((c for o in objs if (c := find_key(o, "search_results_connection"))), None)
    if conn is None:  # never pass a malformed/blocked response off as "no ads"
        raise meta.MetaError(502, "Meta response had no search results block")
    ads, collations, nxt, _ = parse_connection(conn)
    return ads, collations, nxt


CURSOR_PREFIX = "pt."  # our cursor = Meta cursor + how many ads of that page were already returned


def _wrap(meta_cursor, skip):
    return meta_cursor if not skip else CURSOR_PREFIX + base64.urlsafe_b64encode(orjson.dumps([meta_cursor, skip])).decode()


def _unwrap(cursor):
    if not (cursor or "").startswith(CURSOR_PREFIX):
        return cursor, 0  # plain Meta cursor (or none)
    try:
        meta_cursor, skip = orjson.loads(base64.urlsafe_b64decode(cursor[len(CURSOR_PREFIX):]))
        return meta_cursor, int(skip)
    except Exception:
        raise ApiError(400, "bad_request", "cursor is malformed; pass back exactly what we returned")


async def _collect(p, page_id=None):
    """Follow Meta's cursor until `numberOfRecords` ads; the returned cursor resumes right after the last one.
    ponytail: a batch ending mid-page re-fetches that Meta page on the next call; ask in multiples of 10 to avoid it."""
    cursor, skip = _unwrap(p.cursor)
    ads, cols = [], []
    for _ in range(p.numberOfRecords // 10 + 3):  # guard: Meta looping on empty pages can't spin us forever
        page_ads, page_cols, nxt = await _page(p, page_id, cursor)
        page_ads, page_cols = page_ads[skip:], page_cols[skip:]
        want = p.numberOfRecords - len(ads)
        if len(page_ads) > want:
            ads, cols = ads + page_ads[:want], cols + page_cols[:want]
            cursor = _wrap(cursor, skip + want)
            break
        ads, cols, skip = ads + page_ads, cols + page_cols, 0
        cursor = nxt
        if not nxt or len(ads) == p.numberOfRecords:
            break
    return {"ads": ads, "cursor": cursor, "summary": summary(ads, cols)}


def _params(p, **extra):
    return p.model_dump(mode="json", exclude={"trim"}) | extra


def _trim(ads, on):
    return [trim_ad(a) for a in ads] if on else ads


async def company_ads(p: CompanyAdsParams):
    if not p.pageId and not p.companyName:
        raise ApiError(400, "bad_request", "Provide pageId or companyName")
    resolved, page_id = None, p.pageId
    if not page_id:
        resolved = await resolve(p.companyName)
        page_id = resolved["page_id"]
    k = cache.key("company_ads", _params(p, pageId=page_id, companyName=None))
    hit = await cache.get(k, settings.list_cache_seconds)
    data, cached_at = hit if hit else (await _collect(p, page_id), None)
    if not hit:
        await cache.put(k, data)
    out = {"results": _trim(data["ads"], p.trim), "cursor": data["cursor"], "summary": data["summary"]}
    if resolved:
        out["resolved_page"] = resolved
    return envelope(out, cached_at)


async def search_ads(p: SearchAdsParams):
    k = cache.key("search_ads", _params(p))
    hit = await cache.get(k, settings.list_cache_seconds)
    if hit:
        data, cached_at = hit
    else:
        cached_at = None
        page, count = await asyncio.gather(_collect(p), _count(p) if not p.cursor else asyncio.sleep(0))
        data = page | {"count": count}
        await cache.put(k, data)
    return envelope({"searchResults": _trim(data["ads"], p.trim), "searchResultsCount": data["count"],
                     "cursor": data["cursor"], "summary": data["summary"]}, cached_at)


# ---------- single ad ----------

def ad_id_from(id_, url):
    if id_:
        if not id_.isdigit():
            raise ApiError(400, "bad_request", "id must be numeric")
        return id_
    if url and (m := re.search(r"[?&]id=(\d+)", url)):
        return m.group(1)
    raise ApiError(400, "bad_request", "Provide id or a url containing ?id=<ad archive id>")


async def _details(ad_id, max_age):
    k = cache.key("ad", {"id": ad_id})
    if max_age and (hit := await cache.get(k, AGES[max_age])):
        return hit
    res = html_find(await meta.html(f"{LIBRARY}?id={ad_id}"), "deeplink_ad_archive_result")
    if res is None:  # a block/login/challenge page has no ad blob at all — that's not "ad doesn't exist"
        raise meta.MetaError(502, "Meta served a page without ad data (blocked, rate-limited or layout changed)")
    node = res.get("deeplink_ad_archive")
    if not node:  # Meta says so explicitly: no_result_reason = NO_AD_FOUND
        raise ApiError(404, "ad_not_found", f"Ad {ad_id} not found in the Ad Library ({res.get('no_result_reason')})")
    details = None
    try:  # EU/UK transparency + aaa_info live in a second query; the ad itself is already in hand
        political = bool(node.get("political_countries") or (node.get("snapshot") or {}).get("disclaimer_label"))
        objs = await meta.gql("AdLibraryV3AdDetailsQuery", {
            "adArchiveID": ad_id, "pageID": node.get("page_id"), "country": "ALL", "sessionID": session.session_id,
            "source": None, "isAdNonPolitical": not political, "isAdNotAAAEligible": not node.get("is_aaa_eligible")})
        details = next((d for o in objs if (d := find_key(o, "ad_details"))), None)
    except meta.MetaError as e:
        log.warning("ad details query failed for %s: %s", ad_id, e.message)
    data = parse_details(node, details)
    await cache.put(k, data)
    return data, None


async def ad_details(ad_id, max_age, trim):
    data, cached_at = await _details(ad_id, max_age)
    return envelope(trim_ad(data) if trim else data, cached_at)


async def transcript(ad_id, max_age):
    k = cache.key("transcript", {"id": ad_id})
    if max_age and (hit := await cache.get(k, AGES[max_age])):
        return envelope(*hit)
    details, _ = await _details(ad_id, max_age or "1d")
    video = pick_video(details["snapshot"])
    text = await transcribe_url(video) if video else None
    data = {"data": {"ad_id": ad_id, "url": details["url"], "transcript": text or None, "transcript_available": bool(text)}}
    if text is not None or not video:  # don't pin a failed/disabled transcription in cache
        await cache.put(k, data)
    return envelope(data)
