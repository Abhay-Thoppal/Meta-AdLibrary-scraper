"""Routes with the scraper mocked: envelope, auth, 400 shape, GET/POST parity."""
import pytest
from fastapi.testclient import TestClient

from app import main, service
from app.config import settings

B = "/v1/facebook/adLibrary"


@pytest.fixture
def c(monkeypatch):
    async def page(p, page_id, cursor):
        return [], [], "NEXT"

    async def typeahead(q):
        return [{"page_id": "15087023444", "name": "Nike", "page_alias": "nike", "likes": 1, "verification": "BLUE_VERIFIED"}]

    async def no_cache(*a):
        return None

    monkeypatch.setattr(service, "_page", page)
    monkeypatch.setattr(service, "_count", lambda p: _const(42))
    monkeypatch.setattr(service, "typeahead", typeahead)
    monkeypatch.setattr(service.cache, "get", no_cache)
    monkeypatch.setattr(service.cache, "put", no_cache)
    return TestClient(main.app)  # no `with`: lifespan (browser) never starts


async def _const(v):
    return v


def test_company_ads_get_and_post(c):
    g = c.get(f"{B}/company/ads", params={"companyName": "Nike", "country": "IN"}).json()
    p = c.post(f"{B}/company/ads", json={"companyName": "Nike", "country": "IN"}).json()
    assert g == p
    assert g["success"] is True and not any(k.startswith("credits") for k in g)
    assert g["results"] == [] and g["cursor"] == "NEXT" and "summary" in g
    assert g["resolved_page"] == {"page_id": "15087023444", "name": "Nike", "match": "exact", "candidates": 1}
    assert "resolved_page" not in c.get(f"{B}/company/ads", params={"pageId": "15087023444"}).json()


def test_search_ads(c):
    r = c.get(f"{B}/search/ads", params={"query": "running", "search_type": "keyword_exact_phrase"}).json()
    assert r["searchResultsCount"] == 42 and r["searchResults"] == [] and r["cursor"] == "NEXT"


@pytest.mark.parametrize("url", [
    f"{B}/company/ads",                                  # neither pageId nor companyName
    f"{B}/company/ads?pageId=abc",                       # non-numeric pageId
    f"{B}/company/ads?pageId=1&start_date=2025-02-30",   # bad date
    f"{B}/company/ads?pageId=1&media_type=GIF",          # bad enum
    f"{B}/search/ads",                                   # missing query
    f"{B}/ad",                                           # neither id nor url
    f"{B}/ad?id=1&cache_max_age=2d",                     # bad enum
])
def test_400_shape(c, url):
    r = c.get(url)
    assert r.status_code == 400
    assert r.json()["success"] is False and r.json()["error"] == "bad_request" and r.json()["message"]


def test_api_key(c, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "s3cret")
    assert c.get(f"{B}/search/ads?query=x").status_code == 401
    assert c.get(f"{B}/search/ads?query=x", headers={"x-api-key": "s3cret"}).status_code == 200


def test_ad_id_from_url():
    assert service.ad_id_from(None, "https://www.facebook.com/ads/library?id=1185617869915074") == "1185617869915074"
    assert service.ad_id_from(None, "https://www.facebook.com/ads/library/?active_status=all&id=42&x=1") == "42"


def test_docs_list_routes(c):
    paths = c.get("/openapi.json").json()["paths"]
    assert set(paths) == {f"{B}/search/companies", f"{B}/company/ads", f"{B}/search/ads", f"{B}/ad",
                          f"{B}/ad/transcript", "/health"}
    assert set(paths[f"{B}/company/ads"]) == {"get", "post"}


def test_blank_params_fall_back_to_defaults(c):
    r = c.get(f"{B}/company/ads", params={"pageId": "1", "cursor": "", "country": "", "language": ""})
    assert r.status_code == 200 and r.json()["cursor"] == "NEXT"


async def test_rate_limit_goes_to_browser_and_stays_there(monkeypatch):
    from app.scraper import graphql as meta
    sent, browser = [], []
    limited = '{"errors":[{"message":"Rate limit exceeded","severity":"CRITICAL","code":1675004}]}'

    async def send(url, form):
        sent.append(1)
        return 200, limited

    async def browser_fetch(url, form):
        browser.append(1)
        return 200, '{"data":{"ok":1}}'

    async def noop(force=False):
        if force:
            raise AssertionError("must not re-bootstrap on a rate limit")

    monkeypatch.setattr(meta, "_send", send)
    monkeypatch.setattr(meta, "_browser_until", 0.0)
    monkeypatch.setattr(meta.session, "ensure", noop)
    monkeypatch.setattr(meta.session, "browser_fetch", browser_fetch)
    monkeypatch.setattr(meta.session, "doc_ids", {"Q": "1"})
    assert (await meta.gql("Q", {}))[0]["data"] == {"ok": 1}
    assert (sent, browser) == ([1], [1])            # one direct hit, no retries, straight to browser
    await meta.gql("Q", {})
    assert (sent, browser) == ([1], [1, 1])         # cooldown: direct path skipped entirely


def fake_meta(total=35, per_page=10):
    """Fake Meta: ads 0..total-1, 10 per page; cursor 'c<offset>'. Records every page request."""
    calls = []

    async def page(p, page_id, cursor):
        start = int(cursor[1:]) if cursor else 0
        calls.append(start)
        ids = range(start, min(start + per_page, total))
        ads = [{"ad_archive_id": str(i), "is_active": True, "start_date": 0, "publisher_platform": [],
                "stats": {"ad_type": "IMAGE", "duration": {"days_running": 1},
                          "creative": {"landing_domain": None}}} for i in ids]
        nxt = f"c{start + per_page}" if start + per_page < total else None
        return ads, [None] * len(ads), nxt
    return page, calls


@pytest.mark.parametrize("n", [1, 10, 15, 20, 7, 35, 100])
async def test_number_of_records_walks_every_ad_exactly_once(monkeypatch, n):
    from app.models import CompanyAdsParams
    page, calls = fake_meta()
    monkeypatch.setattr(service, "_page", page)
    seen, cursor = [], None
    while True:
        out = await service._collect(CompanyAdsParams(pageId="1", numberOfRecords=n, cursor=cursor))
        assert len(out["ads"]) == min(n, 35 - len(seen))          # exact batch size until the tail
        assert out["summary"]["ads_in_page"] == len(out["ads"])
        seen += [a["ad_archive_id"] for a in out["ads"]]
        cursor = out["cursor"]
        if not cursor:
            break
    assert seen == [str(i) for i in range(35)]                     # no gaps, no duplicates, in order


async def test_plain_meta_cursor_and_bad_cursor(monkeypatch):
    from app.models import CompanyAdsParams
    page, calls = fake_meta()
    monkeypatch.setattr(service, "_page", page)
    out = await service._collect(CompanyAdsParams(pageId="1", cursor="c10"))  # raw Meta cursor still works
    assert [a["ad_archive_id"] for a in out["ads"]][0] == "10" and out["cursor"] == "c20"
    with pytest.raises(service.ApiError):
        await service._collect(CompanyAdsParams(pageId="1", cursor="pt.not-base64!!"))


def test_number_of_records_bounds(c):
    assert c.get(f"{B}/company/ads", params={"pageId": "1", "numberOfRecords": 101}).status_code == 400
    assert c.get(f"{B}/company/ads", params={"pageId": "1", "numberOfRecords": 0}).status_code == 400


@pytest.mark.parametrize("html,status", [
    ("<html>login wall / checkpoint</html>", 502),   # blocked page: must NOT claim the ad doesn't exist
    ('<script type="application/json">{"deeplink_ad_archive_result":{"deeplink_ad_archive":null,'
     '"no_result_reason":"NO_AD_FOUND"}}</script>', 404),
])
def test_ad_details_blocked_vs_missing(c, monkeypatch, html, status):
    async def fake_html(url):
        return html
    monkeypatch.setattr(service.meta, "html", fake_html)
    r = c.get(f"{B}/ad?id=123")
    assert r.status_code == status and r.json()["success"] is False


def test_critical_error_next_to_data_is_an_error():
    from app.scraper.parsers import meta_error
    assert "1675004" in meta_error([{"data": {"ad_library_main": None},
                                     "errors": [{"message": "Rate limit exceeded", "severity": "CRITICAL", "code": 1675004}]}])
    assert meta_error([{"data": {"x": 1}, "errors": [{"message": "deprecated field", "severity": "WARNING"}]}]) is None


@pytest.fixture
def stale(monkeypatch):
    """Fake Meta that rejects doc_id 'old'; `repairs` counts re-captures; `sent` records doc_ids used."""
    from app.scraper import graphql as meta
    sent, repairs = [], []

    async def fetch(url, form_fn):
        f = form_fn()
        sent.append(f["doc_id"])
        if f["doc_id"] == "old":
            raise meta.MetaError(502, "Meta error 1357054: Your request couldn't be processed")
        return [{"data": {"ok": 1}}]

    monkeypatch.setattr(meta, "_fetch", fetch)
    monkeypatch.setattr(meta.session, "doc_ids", {"Q": "old"})
    monkeypatch.setattr(meta.session, "form", {})
    return meta, sent, repairs


async def test_stale_doc_id_is_repaired_and_retried(stale, monkeypatch):
    meta, sent, repairs = stale

    async def repair():
        repairs.append(1)
        meta.session.doc_ids["Q"] = "new"
    monkeypatch.setattr(meta.session, "repair_doc_ids", repair)
    assert (await meta.gql("Q", {}))[0]["data"] == {"ok": 1}
    assert sent == ["old", "new"] and repairs == [1]


async def test_unchanged_doc_id_surfaces_original_error(stale, monkeypatch):
    meta, sent, repairs = stale

    async def repair():
        repairs.append(1)  # Meta still says the same id: not a doc_id problem
    monkeypatch.setattr(meta.session, "repair_doc_ids", repair)
    with pytest.raises(meta.MetaError, match="1357054"):
        await meta.gql("Q", {})
    assert sent == ["old"] and repairs == [1]  # no pointless retry


async def test_rate_limit_never_triggers_repair(monkeypatch):
    from app.scraper import graphql as meta

    async def fetch(url, form_fn):
        raise meta.MetaError(429, "Meta error 1675004 Rate limit exceeded")

    async def repair():
        raise AssertionError("rate limits must not trigger a re-capture")
    monkeypatch.setattr(meta, "_fetch", fetch)
    monkeypatch.setattr(meta.session, "repair_doc_ids", repair)
    with pytest.raises(meta.MetaError):
        await meta.gql("Q", {})


async def test_repair_cooldown(monkeypatch):
    import time
    from app.scraper.session import Session
    s = Session()
    s.last_repair = time.time()  # just repaired: must return without touching the (absent) browser
    await s.repair_doc_ids()
    assert s.page is None


async def test_document_not_found_fails_fast(monkeypatch):
    """Meta's exact stale-doc_id error: no retries, no re-bootstrap, no browser — straight back to gql() for repair."""
    from app.scraper import graphql as meta
    sent = []

    async def send(url, form):
        sent.append(1)
        return 200, '{"errors":[{"message":"The GraphQL document with ID 111 was not found.","severity":"CRITICAL"}]}'

    async def boom(*a, **k):
        raise AssertionError("must not re-bootstrap or use the browser for a missing document")
    monkeypatch.setattr(meta, "_send", send)
    monkeypatch.setattr(meta, "_browser_until", 0.0)
    monkeypatch.setattr(meta.session, "ensure", lambda force=False: _noop(force, boom))
    monkeypatch.setattr(meta.session, "browser_fetch", boom)
    with pytest.raises(meta.MetaError, match="was not found"):
        await meta._fetch(meta.GRAPHQL, lambda: {"doc_id": "111"})
    assert sent == [1]


async def _noop(force, boom):
    if force:
        await boom()
