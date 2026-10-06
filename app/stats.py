"""§6 — per-ad `stats` and list `summary`. Pure: raw Meta node in, numbers out. Unknown stays None."""
import re
import statistics
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse


def iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _num(s):
    m = re.match(r"([\d.,]+)\s*([KMB]?)", s.strip().upper())
    if not m:
        return None
    return round(float(m.group(1).replace(",", "")) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])


def parse_range(v):
    """Meta range ('1K-2K', '<1K', '>1M', '₹100 - ₹199', {lower_bound, upper_bound}) -> (lo, hi)."""
    if v is None or v == "":
        return None, None
    if isinstance(v, dict):
        lo, hi = v.get("lower_bound"), v.get("upper_bound")
        return (int(lo) if lo not in (None, "") else None), (int(hi) if hi not in (None, "") else None)
    if isinstance(v, (int, float)):
        return int(v), int(v)
    s = re.sub(r"[^\d.,KMBkmb<>\-–]", "", str(v))
    if s.startswith("<"):
        return 0, _num(s[1:])
    if s.startswith(">"):
        return _num(s[1:]), None
    parts = [p for p in re.split(r"[-–]", s) if p]
    nums = [_num(p) for p in parts]
    return (nums[0], nums[-1]) if nums else (None, None)


def body_text(snap):
    b = snap.get("body")
    t = b.get("text") if isinstance(b, dict) else b
    if not t and snap.get("cards"):
        t = snap["cards"][0].get("body")
    return t or ""


def ad_type(fmt, snap):
    cards = snap.get("cards") or []
    if fmt == "DPA":
        return "DPA"
    if fmt == "DCO" or snap.get("dynamic_versions"):
        return "DCO"
    if fmt in ("CAROUSEL", "MULTI_IMAGES") or len(cards) > 1:
        return "CAROUSEL"
    if fmt == "VIDEO" or snap.get("videos"):
        return "VIDEO"
    if fmt == "IMAGE" or snap.get("images"):
        return "IMAGE"
    if fmt == "EVENT" or snap.get("event"):
        return "EVENT"
    if fmt == "TEXT" or body_text(snap):
        return "TEXT"
    return "UNKNOWN"


def ad_stats(node, transparency=None):
    """node: raw Meta ad node (snake_case, as in search results / deeplink). transparency: eu/uk block from details."""
    snap = node.get("snapshot") or {}
    fmt = (snap.get("display_format") or "").upper() or None
    cards = snap.get("cards") or []
    start, end = node.get("start_date"), node.get("end_date")
    dur = end - start if start is not None and end is not None else None
    typ = ad_type(fmt, snap)
    link = snap.get("link_url") or next((c.get("link_url") for c in cards if c.get("link_url")), None)
    url = urlparse(link) if link else None
    body = body_text(snap)
    title = snap.get("title") or (cards[0].get("title") if cards else None) or ""
    cats = snap.get("page_categories")
    countries = node.get("targeted_or_reached_countries") or []
    tr = transparency or {}
    locs = [l for l in tr.get("location_audience") or [] if not l.get("excluded")]
    age = tr.get("age_audience") or {}
    imp_lo, imp_hi = parse_range((node.get("impressions_with_index") or {}).get("impressions_text"))
    spend_lo, spend_hi = parse_range(node.get("spend"))
    eac = snap.get("effective_authorization_category")
    platforms = node.get("publisher_platform") or []
    return {
        "ad_type": typ,
        "ad_type_detail": {
            "display_format_raw": snap.get("display_format"),
            "is_carousel": typ == "CAROUSEL",
            "is_dynamic_creative": fmt == "DCO",
            "is_catalog_dpa": fmt == "DPA",
            "has_multiple_versions": fmt == "DCO" or bool(snap.get("dynamic_versions")),
            "is_political_or_issue": bool(node.get("political_countries"))
            or (eac not in (None, "NONE"))
            or bool(snap.get("disclaimer_label") or snap.get("byline")),
            "is_reshared_post": bool(snap.get("is_reshared")),
        },
        "duration": {
            "start_date": start,
            "end_date": end,
            "start_date_string": iso(start),
            "end_date_string": iso(end),
            "is_ongoing": node.get("is_active"),
            "days_running": dur // 86400 + 1 if dur is not None else None,
            "duration_seconds": dur,
            "duration_human": f"{dur // 86400}d {dur % 86400 // 3600}h" if dur is not None else None,
            "total_active_time_seconds": node.get("total_active_time"),
        },
        "platforms": {"list": platforms, "count": len(platforms)},
        "creative": {
            "image_count": len(snap.get("images") or []),
            "video_count": len(snap.get("videos") or []),
            "card_count": len(cards),
            "extra_image_count": len(snap.get("extra_images") or []),
            "extra_video_count": len(snap.get("extra_videos") or []),
            "extra_link_count": len(snap.get("extra_links") or []),
            "variant_count": node.get("collation_count") or (max(1, len(cards)) if typ == "DCO" else 1),
            "body_char_count": len(body),
            "body_word_count": len(body.split()),
            "title_char_count": len(title),
            "has_cta": bool(snap.get("cta_type") and snap.get("cta_type") != "NO_BUTTON"),
            "cta_type": snap.get("cta_type"),
            "landing_domain": url.netloc.removeprefix("www.") or None if url else None,
            "has_utm_params": any(k.startswith("utm_") for k in parse_qs(url.query)) if url else False,
        },
        "page": {
            "page_like_count": snap.get("page_like_count"),
            "page_categories": list(cats.values()) if isinstance(cats, dict) else cats or [],
        },
        "delivery": {
            "reach_estimate": node.get("reach_estimate"),
            "eu_total_reach": tr.get("eu_total_reach"),
            "impressions_lower": imp_lo,
            "impressions_upper": imp_hi,
            "spend_lower": spend_lo,
            "spend_upper": spend_hi,
            "currency": node.get("currency") or None,
            "targeted_countries_count": len(countries) or len(locs) or None,
            "age_min": age.get("min"),
            "age_max": age.get("max"),
            "gender": tr.get("gender_audience"),
        },
    }


def summary(ads, collation_ids):
    """§6.3 over one page. ads: parsed list-shape ads; collation_ids: raw collation_id per ad (None = unique)."""
    days = [a["stats"]["duration"]["days_running"] for a in ads if a["stats"]["duration"]["days_running"] is not None]
    starts = sorted(a["start_date"] for a in ads if a.get("start_date") is not None)
    active = sum(1 for a in ads if a.get("is_active"))
    domains = {a["stats"]["creative"]["landing_domain"] for a in ads} - {None}
    return {
        "ads_in_page": len(ads),
        "active_count": active,
        "inactive_count": len(ads) - active,
        "by_ad_type": dict(Counter(a["stats"]["ad_type"] for a in ads)),
        "by_platform": dict(Counter(p for a in ads for p in a.get("publisher_platform") or [])),
        "days_running": {
            "min": min(days), "max": max(days),
            "avg": round(sum(days) / len(days), 1), "median": statistics.median(days),
        } if days else {"min": None, "max": None, "avg": None, "median": None},
        "oldest_start_date_string": iso(starts[0]) if starts else None,
        "newest_start_date_string": iso(starts[-1]) if starts else None,
        "distinct_landing_domains": len(domains),
        "distinct_creatives": len({c or a["ad_archive_id"] for a, c in zip(ads, collation_ids)}),
    }
