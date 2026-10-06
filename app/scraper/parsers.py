"""Pure parsers: raw Meta payloads -> API response shapes. Built from tests/fixtures captures."""
import re

import orjson

from app.stats import ad_stats, iso

AD_URL = "https://www.facebook.com/ads/library?id={}"

# §5.2 list snapshot keys and their empty value when Meta omits them
LIST_SNAPSHOT = dict.fromkeys(
    "body branded_content brazil_tax_id byline caption cta_text cta_type country_iso_code current_page_name "
    "disclaimer_label display_format event is_reshared link_description link_url page_entity_type page_id "
    "page_is_deleted page_is_profile_page page_like_count page_name page_profile_picture_url page_profile_uri "
    "root_reshared_post title additional_info".split()
) | {k: [] for k in "cards images videos page_categories ec_certificates extra_images extra_links extra_texts extra_videos".split()}

TYPEAHEAD_KEYS = ("page_id category image_uri likes verification name country entity_type ig_username "
                  "ig_followers ig_verification page_alias page_is_deleted").split()


def graphql_lines(text):
    """Meta may prefix `for (;;);` and send several JSON objects separated by newlines."""
    return [orjson.loads(l) for l in text.removeprefix("for (;;);").splitlines() if l.strip()]


def meta_error(objs):
    """Return Meta's error text if the payload is an error, else None."""
    for o in objs:
        if o.get("error"):
            return f"{o.get('error')}: {o.get('errorSummary') or ''} {o.get('errorDescription') or ''}".strip()
        errs = o.get("errors") or []
        # errors next to data are usually harmless warnings — but a CRITICAL one (e.g. rate limit) never is
        if errs and (not o.get("data") or any(e.get("severity") == "CRITICAL" for e in errs)):
            return "; ".join(f"{e.get('code', '')} {e.get('message', '')}".strip() for e in errs)
    return None


def find_key(obj, key):
    """Depth-first search for the first value stored under `key`."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if key in o:
                return o[key]
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return None


def html_find(html, key):
    """Find `key` inside the server-rendered <script type="application/json"> blobs of an Ad Library page."""
    for m in re.finditer(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
        if key not in m.group(1):
            continue
        try:
            v = find_key(orjson.loads(m.group(1)), key)
        except orjson.JSONDecodeError:
            continue
        if v is not None:
            return v
    return None


def parse_ad(node):
    """Raw ad node -> §5.2 ad dict."""
    snap = node.get("snapshot") or {}
    out_snap = LIST_SNAPSHOT | snap
    out_snap["current_page_name"] = out_snap["current_page_name"] or snap.get("page_name")
    for k, empty in LIST_SNAPSHOT.items():
        if out_snap[k] is None and empty == []:
            out_snap[k] = []
    aid = node.get("ad_archive_id")
    return {
        "ad_archive_id": aid,
        "end_date": node.get("end_date"),
        "is_aaa_eligible": node.get("is_aaa_eligible"),
        "is_active": node.get("is_active"),
        "page_id": node.get("page_id"),
        "page_name": node.get("page_name") or snap.get("page_name"),
        "political_countries": node.get("political_countries") or [],
        "reach_estimate": node.get("reach_estimate"),
        "snapshot": out_snap,
        "total_active_time": node.get("total_active_time"),
        "spend": node.get("spend"),
        "start_date": node.get("start_date"),
        "publisher_platform": node.get("publisher_platform") or [],
        "url": AD_URL.format(aid),
        "start_date_string": iso(node.get("start_date")),
        "end_date_string": iso(node.get("end_date")),
        "stats": ad_stats(node),
    }


def parse_connection(conn):
    """search_results_connection -> (ads, collation_ids, cursor, count). collated_results flattened."""
    nodes = [n for e in (conn or {}).get("edges") or [] for n in (e.get("node") or {}).get("collated_results") or []]
    info = (conn or {}).get("page_info") or {}
    cursor = info.get("end_cursor") if info.get("has_next_page") else None
    return [parse_ad(n) for n in nodes], [n.get("collation_id") for n in nodes], cursor, (conn or {}).get("count")


def parse_typeahead(objs):
    for o in objs:
        pages = find_key(o, "page_results")
        if pages is not None:
            return [{k: p.get(k) for k in TYPEAHEAD_KEYS} for p in pages]
    return []


def parse_details(node, details=None):
    """Deeplink ad node (+ optional AdLibraryV3AdDetailsQuery `ad_details`) -> §5.5 camelCase dict."""
    details = details or {}
    snap = node.get("snapshot") or {}
    tr = details.get("transparency_by_location") or {}
    eu, uk = tr.get("eu_transparency"), tr.get("uk_transparency")
    page_info = find_key(details, "page_info") or {}
    body = snap.get("body")
    cats = snap.get("page_categories") or []
    to_int = lambda v: int(v) if v not in (None, "") else None  # noqa: E731
    images = [i | {"image_crops": i.get("image_crops") or {}} for i in snap.get("images") or []]
    out_snap = {
        "ad_creative_id": None, "cards": snap.get("cards") or [], "body_translations": {},
        "byline": snap.get("byline"), "caption": snap.get("caption"), "cta_text": snap.get("cta_text"),
        "dynamic_item_flags": {}, "dynamic_versions": snap.get("dynamic_versions"), "edited_snapshots": [],
        "effective_authorization_category": snap.get("effective_authorization_category"),
        "event": snap.get("event") or [], "extra_images": snap.get("extra_images") or [],
        "extra_links": snap.get("extra_links") or [], "extra_texts": snap.get("extra_texts") or [],
        "extra_videos": snap.get("extra_videos") or [], "instagram_shopping_products": [],
        "display_format": (snap.get("display_format") or "").lower() or None, "title": snap.get("title"),
        "link_description": snap.get("link_description"), "link_url": snap.get("link_url"),
        "page_welcome_message": None, "images": images, "videos": snap.get("videos") or [],
        "creation_time": None, "page_id": to_int(snap.get("page_id") or node.get("page_id")),
        "page_name": snap.get("page_name"), "page_profile_picture_url": snap.get("page_profile_picture_url"),
        # ponytail: Meta only gives category names here, so keys are the names; switch to ids if Meta ever exposes them
        "page_categories": cats if isinstance(cats, dict) else {c: c for c in cats},
        "page_entity_type": (page_info.get("entity_type") or snap.get("page_entity_type") or "").lower() or None,
        "page_is_profile_page": page_info.get("is_profile_page", snap.get("page_is_profile_page")),
        "instagram_actor_name": None, "instagram_profile_pic_url": None, "instagram_url": None,
        "instagram_handle": None, "is_reshared": snap.get("is_reshared"), "version": None,
        "body": body.get("text") if isinstance(body, dict) else body,
        "brazil_tax_id": snap.get("brazil_tax_id"), "branded_content": snap.get("branded_content"),
        "current_page_name": snap.get("current_page_name") or snap.get("page_name"),
        "disclaimer_label": snap.get("disclaimer_label"), "page_like_count": snap.get("page_like_count"),
        "page_profile_uri": snap.get("page_profile_uri"), "page_is_deleted": snap.get("page_is_deleted"),
        "root_reshared_post": snap.get("root_reshared_post"), "cta_type": snap.get("cta_type"),
        "additional_info": snap.get("additional_info"), "ec_certificates": snap.get("ec_certificates"),
        "country_iso_code": snap.get("country_iso_code"), "instagram_branded_content": None,
    }
    aaa = None
    if details.get("aaa_info") or eu or uk:
        eu = eu or {}
        aaa = (details.get("aaa_info") or {}) | {
            "location_audience": eu.get("location_audience") or [],
            "age_country_gender_reach_breakdown": eu.get("age_country_gender_reach_breakdown") or [],
            "eu_total_reach": eu.get("eu_total_reach"),
            "gender_audience": eu.get("gender_audience"),
            "age_audience": eu.get("age_audience"),
            "uk_transparency": uk,
        }
    aid = node.get("ad_archive_id")
    return {
        "adArchiveID": to_int(aid), "endDate": node.get("end_date"), "isAAAEligible": node.get("is_aaa_eligible"),
        "isActive": node.get("is_active"), "pageID": to_int(node.get("page_id")),
        "pageName": node.get("page_name") or snap.get("page_name"),
        "politicalCountries": node.get("political_countries") or [], "reachEstimate": node.get("reach_estimate"),
        "snapshot": out_snap, "totalActiveTime": node.get("total_active_time"), "spend": node.get("spend"),
        "startDate": node.get("start_date"),
        "publisherPlatform": [p.lower() for p in node.get("publisher_platform") or []],
        "url": AD_URL.format(aid), "startDateString": iso(node.get("start_date")),
        "endDateString": iso(node.get("end_date")), "aaa_info": aaa,
        "stats": ad_stats(node, eu or uk),
    }
