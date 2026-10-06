"""Hits the real Meta Ad Library. `uv run pytest -m live`."""
import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.live
B = "/v1/facebook/adLibrary"


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as client:  # runs lifespan -> real browser session
        yield client


def test_companies(c):
    r = c.get(f"{B}/search/companies", params={"query": "nike"}).json()
    assert r["success"] and r["searchResults"][0]["page_id"]


def test_company_ads_paging(c):
    p1 = c.get(f"{B}/company/ads", params={"companyName": "Nike", "country": "IN"}).json()
    assert p1["resolved_page"]["page_id"] == "15087023444" and p1["results"] and p1["cursor"]
    assert all("stats" in a for a in p1["results"]) and p1["summary"]["ads_in_page"] == len(p1["results"])
    p2 = c.post(f"{B}/company/ads", json={"pageId": "15087023444", "country": "IN", "cursor": p1["cursor"]}).json()
    ids1 = {a["ad_archive_id"] for a in p1["results"]}
    assert p2["results"] and not ids1 & {a["ad_archive_id"] for a in p2["results"]}


def test_search_and_details(c):
    s = c.get(f"{B}/search/ads", params={"query": "running", "search_type": "keyword_exact_phrase", "country": "US"}).json()
    assert s["searchResults"] and s["searchResultsCount"]
    ad_id = s["searchResults"][0]["ad_archive_id"]
    d = c.get(f"{B}/ad", params={"url": f"https://www.facebook.com/ads/library?id={ad_id}"}).json()
    assert d["adArchiveID"] == int(ad_id) and isinstance(d["snapshot"]["body"], (str, type(None)))
    assert c.get(f"{B}/ad", params={"id": ad_id, "cache_max_age": "1d"}).json()["cached"] is True


def test_transcript_image_ad_is_graceful(c):
    s = c.get(f"{B}/company/ads", params={"pageId": "15087023444", "media_type": "IMAGE", "country": "IN"}).json()
    ad = next((a for a in s["results"] if not a["snapshot"]["videos"] and not any(
        x.get("video_sd_url") or x.get("video_hd_url") for x in a["snapshot"]["cards"])), None)
    if not ad:
        pytest.skip("no pure image ad on this page right now")
    t = c.get(f"{B}/ad/transcript", params={"id": ad["ad_archive_id"]}).json()["data"]
    assert t["transcript"] is None and t["transcript_available"] is False
