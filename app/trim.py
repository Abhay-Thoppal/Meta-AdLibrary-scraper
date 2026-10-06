"""§8 — trim=true shape. Pure."""

LIST_KEYS = ("ad_archive_id page_id page_name is_active start_date end_date start_date_string end_date_string "
             "publisher_platform url stats").split()
DETAIL_KEYS = ("adArchiveID pageID pageName isActive startDate endDate startDateString endDateString "
               "publisherPlatform url stats").split()
SNAP_KEYS = "body title caption cta_text cta_type link_url display_format".split()
SUB = {
    "images": ("original_image_url",),
    "videos": ("video_hd_url", "video_sd_url", "video_preview_image_url"),
    "cards": ("body", "title", "link_url", "original_image_url", "video_hd_url"),
}


def _snap(s):
    out = {k: s.get(k) for k in SNAP_KEYS}
    for k, keep in SUB.items():
        out[k] = [{f: i.get(f) for f in keep} for i in s.get(k) or []]
    return out


def trim_ad(ad):
    keys = DETAIL_KEYS if "adArchiveID" in ad else LIST_KEYS
    return {k: ad.get(k) for k in keys} | {"snapshot": _snap(ad.get("snapshot") or {})}
