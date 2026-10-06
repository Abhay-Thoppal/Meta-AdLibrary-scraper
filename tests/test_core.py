"""Parsers, stats, trim and resolver — pure functions against real captured payloads."""
import json
from pathlib import Path

import pytest

from app.scraper.parsers import (graphql_lines, html_find, meta_error, parse_connection, parse_details,
                                 parse_typeahead)
from app.service import pick_page
from app.stats import ad_stats, iso, parse_range, summary
from app.trim import trim_ad

FIX = Path(__file__).parent / "fixtures"


def capture(name):
    """Body of the first captured GraphQL call with this friendly name and a non-trivial payload."""
    for f in sorted(FIX.glob(f"gql_*_{name}.json")):
        body = json.loads(f.read_text(encoding="utf-8"))["body"]
        if len(body) > 1000:
            return graphql_lines(body)
    pytest.skip(f"no {name} fixture; run scripts/discover.py")


def first_page():
    objs = capture("AdLibrarySearchPaginationQuery")
    return parse_connection(objs[0]["data"]["ad_library_main"]["search_results_connection"])


def test_search_page_shape():
    ads, collations, cursor, _ = first_page()
    assert ads and cursor and len(collations) == len(ads)
    a = ads[0]
    assert list(a)[:3] == ["ad_archive_id", "end_date", "is_aaa_eligible"]
    assert isinstance(a["ad_archive_id"], str) and isinstance(a["start_date"], int)
    assert a["start_date_string"].endswith(".000Z")
    assert a["url"] == f"https://www.facebook.com/ads/library?id={a['ad_archive_id']}"
    assert isinstance(a["snapshot"]["body"], dict) and isinstance(a["snapshot"]["page_categories"], list)
    for k in ("cards", "images", "videos", "extra_videos", "political_countries"):
        assert isinstance(a["snapshot"].get(k, a.get(k)), list)
    assert "collation_id" not in a and "stats" in a


def test_typeahead_shape():
    rows = parse_typeahead(capture("useAdLibraryTypeaheadSuggestionDataSourceQuery"))
    assert rows and set(rows[0]) == {"page_id", "category", "image_uri", "likes", "verification", "name", "country",
                                     "entity_type", "ig_username", "ig_followers", "ig_verification", "page_alias",
                                     "page_is_deleted"}


def test_details_eu_shape():
    if not (FIX / "ad_detail_eu.html").exists():
        pytest.skip("no ad_detail_eu.html fixture; run scripts/discover.py")
    html = (FIX / "ad_detail_eu.html").read_text(encoding="utf-8")
    node = html_find(html, "deeplink_ad_archive_result")["deeplink_ad_archive"]
    details = None
    for f in sorted(FIX.glob("gql_*_AdLibraryV3AdDetailsQuery.json")):
        body = json.loads(f.read_text(encoding="utf-8"))
        if json.loads(body["variables"])["adArchiveID"] == node["ad_archive_id"]:
            details = graphql_lines(body["body"])[0]["data"]["ad_library_main"]["ad_details"]
    d = parse_details(node, details)
    assert isinstance(d["adArchiveID"], int) and isinstance(d["pageID"], int)
    assert isinstance(d["snapshot"]["body"], str) and isinstance(d["snapshot"]["page_categories"], dict)
    assert d["snapshot"]["display_format"] == d["snapshot"]["display_format"].lower()
    assert all(p == p.lower() for p in d["publisherPlatform"])
    if details:
        assert d["aaa_info"]["location_audience"] and d["stats"]["delivery"]["eu_total_reach"] > 0


def test_graphql_lines_and_errors():
    objs = graphql_lines('for (;;);{"error":1357054,"errorSummary":"nope"}\n{"data":{}}')
    assert len(objs) == 2 and "1357054" in meta_error(objs)
    assert meta_error([{"data": {"x": 1}}]) is None


def test_parse_range_real_formats():
    assert parse_range("$600K-$700K") == (600_000, 700_000)
    assert parse_range(">$1M") == (1_000_000, None)
    assert parse_range("<1K") == (0, 1000)
    assert parse_range("₹100 - ₹199") == (100, 199)
    assert parse_range({"lower_bound": "100", "upper_bound": "199"}) == (100, 199)
    assert parse_range(None) == (None, None)


def node(**kw):
    snap = kw.pop("snapshot", {})
    return {"ad_archive_id": "1", "is_active": True, "start_date": 1750143600, "end_date": 1750230000,
            "publisher_platform": ["FACEBOOK", "INSTAGRAM"], "snapshot": snap, **kw}


def test_stats_rules():
    s = ad_stats(node(snapshot={"display_format": "VIDEO", "videos": [{}], "body": {"text": "hello big world"},
                                "link_url": "https://www.nike.com/x?utm_source=fb", "cta_type": "LEARN_MORE"}))
    assert s["ad_type"] == "VIDEO"
    assert s["duration"]["duration_seconds"] == 86400 and s["duration"]["days_running"] == 2
    assert s["duration"]["duration_human"] == "1d 0h" and s["duration"]["is_ongoing"] is True
    assert s["creative"]["landing_domain"] == "nike.com" and s["creative"]["has_utm_params"]
    assert s["creative"]["body_word_count"] == 3 and s["platforms"]["count"] == 2
    assert all(v is None for v in s["delivery"].values())  # never invent numbers
    same_day = ad_stats(node(end_date=1750143600))
    assert same_day["duration"]["days_running"] == 1
    # precedence DPA > DCO > CAROUSEL > VIDEO > IMAGE > TEXT
    assert ad_stats(node(snapshot={"display_format": "DPA", "cards": [{}, {}]}))["ad_type"] == "DPA"
    dco = ad_stats(node(snapshot={"display_format": "DCO", "cards": [{}, {}, {}]}))
    assert dco["ad_type"] == "DCO" and dco["creative"]["variant_count"] == 3
    assert ad_stats(node(snapshot={"display_format": "IMAGE", "cards": [{}, {}]}))["ad_type"] == "CAROUSEL"
    assert ad_stats(node(snapshot={"body": {"text": "hi"}}))["ad_type"] == "TEXT"
    assert ad_stats(node(collation_count=7))["creative"]["variant_count"] == 7
    assert ad_stats(node(snapshot={"byline": "Paid for by X"}))["ad_type_detail"]["is_political_or_issue"]


def test_summary_and_iso():
    ads, collations, _, _ = first_page()
    s = summary(ads, collations)
    assert s["ads_in_page"] == len(ads) == s["active_count"] + s["inactive_count"]
    assert sum(s["by_ad_type"].values()) == len(ads)
    assert iso(1750143600) == "2025-06-17T07:00:00.000Z"
    assert summary([], [])["days_running"]["min"] is None


def test_trim():
    ads, *_ = first_page()
    t = trim_ad(ads[0])
    assert set(t) == {"ad_archive_id", "page_id", "page_name", "is_active", "start_date", "end_date",
                      "start_date_string", "end_date_string", "publisher_platform", "url", "stats", "snapshot"}
    assert all(set(c) <= {"body", "title", "link_url", "original_image_url", "video_hd_url"} for c in t["snapshot"]["cards"])
    d = trim_ad({"adArchiveID": 1, "snapshot": {"images": [{"original_image_url": "u", "resized_image_url": "r"}]}})
    assert "adArchiveID" in d and d["snapshot"]["images"] == [{"original_image_url": "u"}]


def test_pick_page():
    rows = [{"page_id": "1", "name": "Nike Football", "page_alias": "nikefootball", "likes": 9, "verification": "BLUE_VERIFIED"},
            {"page_id": "2", "name": "Nike", "page_alias": "nike", "likes": 5, "verification": "BLUE_VERIFIED"},
            {"page_id": "3", "name": "Fan Page", "page_alias": "nikefans", "likes": 99, "verification": "NOT_VERIFIED"}]
    assert pick_page("nike", rows) == {"page_id": "2", "name": "Nike", "match": "exact", "candidates": 3}
    assert pick_page("@NikeFans", rows)["match"] == "alias"
    assert pick_page("swoosh", rows) == {"page_id": "1", "name": "Nike Football", "match": "best_guess", "candidates": 3}
    assert pick_page("x", []) is None
    # real 'Dettol' typeahead: unverified squatter pages share the exact name; the verified brand must win
    dettol = [{"page_id": "106580305480182", "name": "Dettol", "page_alias": None, "likes": 11059, "verification": "NOT_VERIFIED"},
              {"page_id": "834824183050024", "name": "Dettol", "page_alias": None, "likes": 1, "verification": "NOT_VERIFIED"},
              {"page_id": "308723279160232", "name": "Dettol India", "page_alias": "DettolIndia", "likes": 1023665, "verification": "BLUE_VERIFIED"},
              {"page_id": "191000164248962", "name": "Dettol UK", "page_alias": "DettolUK", "likes": 238134, "verification": "BLUE_VERIFIED"}]
    assert pick_page("Dettol", dettol)["page_id"] == "308723279160232"
    assert pick_page("dettoluk", dettol) == {"page_id": "191000164248962", "name": "Dettol UK", "match": "alias", "candidates": 4}
