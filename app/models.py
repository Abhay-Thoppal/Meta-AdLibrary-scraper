"""Request params (shared by GET query + POST body) and Swagger examples copied from §5."""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CacheAge = Literal["1d", "3d", "7d", "14d", "30d"]
DELIVERY_NOTE = ("`stats.delivery` (spend, impressions, reach) is only filled when Meta discloses it: political/issue "
                 "ads and EU/UK-delivered ads. Most commercial ads (e.g. India-only) have it all `null` — expected.")


class AdFilters(BaseModel):
    country: str = Field("ALL", pattern=r"^([A-Za-z]{2}|ALL)$", description="Single ISO-2 code (`IN`, `US`) or `ALL`")
    status: Literal["ALL", "ACTIVE", "INACTIVE"] = Field("ACTIVE", description="Ad delivery status")
    media_type: Literal["ALL", "IMAGE", "VIDEO", "MEME", "IMAGE_AND_MEME", "NONE"] = Field(
        "ALL", description="MEME = image + text")
    language: str | None = Field(None, pattern=r"^[A-Za-z]{2}$", description="2-letter content language (`EN`, `HI`)")
    sort_by: Literal["total_impressions", "relevancy_monthly_grouped"] = Field(
        "total_impressions", description="`relevancy_monthly_grouped` = most recent")
    start_date: date | None = Field(None, description="YYYY-MM-DD, ads delivered on/after")
    end_date: date | None = Field(None, description="YYYY-MM-DD, ads delivered on/before")
    cursor: str | None = Field(None, description="Opaque; pass back the `cursor` we returned")
    numberOfRecords: int = Field(10, ge=1, le=100, description=(
        "Ads to return (1–100). Meta serves 10 per page, so we follow its cursor until we have this many; "
        "the returned `cursor` continues exactly after the last ad. Each extra 10 = one more call to Meta"))
    trim: bool = Field(False, description="Return the slim ad shape")

    @model_validator(mode="before")
    @classmethod
    def _drop_blanks(cls, data):  # Swagger "send empty value" sends cursor= etc. as "": treat as not given
        return {k: v for k, v in data.items() if v != ""} if isinstance(data, dict) else data


class CompanyAdsParams(AdFilters):
    pageId: str | None = Field(None, pattern=r"^\d+$", description="Ad Library page id. Wins over companyName")
    companyName: str | None = Field(None, min_length=1, description="Company name; resolved to a pageId automatically (verified pages preferred)")


class SearchAdsParams(AdFilters):
    query: str = Field(..., min_length=1, description="Keyword(s)")
    search_type: Literal["keyword_unordered", "keyword_exact_phrase"] = "keyword_unordered"
    ad_type: Literal["all", "political_and_issue_ads"] = "all"


ENVELOPE = {"success": True}
EX_STATS = {
    "ad_type": "VIDEO",
    "ad_type_detail": {"display_format_raw": "VIDEO", "is_carousel": False, "is_dynamic_creative": False,
                       "is_catalog_dpa": False, "has_multiple_versions": False, "is_political_or_issue": False,
                       "is_reshared_post": False},
    "duration": {"start_date": 1750143600, "end_date": 1750230000, "start_date_string": "2025-06-17T07:00:00.000Z",
                 "end_date_string": "2025-06-18T07:00:00.000Z", "is_ongoing": True, "days_running": 2,
                 "duration_seconds": 86400, "duration_human": "1d 0h", "total_active_time_seconds": None},
    "platforms": {"list": ["FACEBOOK", "INSTAGRAM"], "count": 2},
    "creative": {"image_count": 0, "video_count": 1, "card_count": 0, "extra_image_count": 0, "extra_video_count": 1,
                 "extra_link_count": 1, "variant_count": 1, "body_char_count": 87, "body_word_count": 14,
                 "title_char_count": 24, "has_cta": True, "cta_type": "LEARN_MORE", "landing_domain": "nike.com.pe",
                 "has_utm_params": True},
    "page": {"page_like_count": 37497718, "page_categories": ["Ropa deportiva"]},
    "delivery": {"reach_estimate": None, "eu_total_reach": None, "impressions_lower": None, "impressions_upper": None,
                 "spend_lower": None, "spend_upper": None, "currency": None, "targeted_countries_count": None,
                 "age_min": None, "age_max": None, "gender": None},
}
EX_AD = {
    "ad_archive_id": "1212560900568826", "end_date": 1750230000, "is_aaa_eligible": False, "is_active": True,
    "page_id": "15087023444", "page_name": "Nike", "political_countries": [], "reach_estimate": None,
    "snapshot": {
        "body": {"text": "…"}, "branded_content": None, "brazil_tax_id": None, "byline": None, "caption": "fb.com",
        "cards": [], "cta_text": "Learn more", "cta_type": "LEARN_MORE", "country_iso_code": None,
        "current_page_name": "Nike", "disclaimer_label": None, "display_format": "VIDEO", "event": None,
        "images": [], "is_reshared": False, "link_description": None, "link_url": "https://…",
        "page_categories": ["Ropa deportiva"], "page_entity_type": None, "page_id": "15087023444",
        "page_is_deleted": False, "page_is_profile_page": None, "page_like_count": 37497718, "page_name": "Nike",
        "page_profile_picture_url": "…", "page_profile_uri": "https://facebook.com/nike", "root_reshared_post": None,
        "title": "Look de invierno activado",
        "videos": [{"video_hd_url": "…", "video_preview_image_url": "…", "video_sd_url": "…",
                    "watermarked_video_hd_url": "", "watermarked_video_sd_url": ""}],
        "additional_info": None, "ec_certificates": [], "extra_images": [], "extra_links": [], "extra_texts": [],
        "extra_videos": [],
    },
    "total_active_time": None, "spend": None, "start_date": 1750143600, "publisher_platform": ["FACEBOOK"],
    "url": "https://www.facebook.com/ads/library?id=1212560900568826",
    "start_date_string": "2025-06-17T07:00:00.000Z", "end_date_string": "2025-06-18T07:00:00.000Z",
    "stats": EX_STATS,
}
EX_SUMMARY = {
    "ads_in_page": 30, "active_count": 28, "inactive_count": 2, "by_ad_type": {"VIDEO": 12, "IMAGE": 14, "CAROUSEL": 4},
    "by_platform": {"FACEBOOK": 30, "INSTAGRAM": 25}, "days_running": {"min": 1, "max": 210, "avg": 34.6, "median": 21},
    "oldest_start_date_string": "2024-11-20T08:00:00.000Z", "newest_start_date_string": "2025-06-17T07:00:00.000Z",
    "distinct_landing_domains": 3, "distinct_creatives": 22,
}
EXAMPLES = {
    "companies": ENVELOPE | {"searchResults": [{
        "page_id": "51212153078", "category": "Product/service", "image_uri": "https://…", "likes": 41136495,
        "verification": "BLUE_VERIFIED", "name": "Nike Football", "country": None, "entity_type": "PERSON_PROFILE",
        "ig_username": "nikefootball", "ig_followers": 46451228, "ig_verification": True,
        "page_alias": "nikefootball", "page_is_deleted": False}]},
    "company_ads": ENVELOPE | {"results": [EX_AD], "cursor": "AQHR…", "summary": EX_SUMMARY,
                               "resolved_page": {"page_id": "15087023444", "name": "Nike", "match": "exact",
                                                 "candidates": 3}},
    "search_ads": ENVELOPE | {"searchResults": [EX_AD], "searchResultsCount": 50001, "cursor": "AQHR…",
                              "summary": EX_SUMMARY},
    "ad": ENVELOPE | {
        "adArchiveID": 1185617869915074, "endDate": 1750316400, "isAAAEligible": False, "isActive": True,
        "pageID": 15087023444, "pageName": "Nike", "politicalCountries": [], "reachEstimate": None,
        "snapshot": {"ad_creative_id": None, "cards": [], "body": "Aprovechá 6 cuotas…", "display_format": "image",
                     "page_categories": {"Sportswear": "Sportswear"}, "page_entity_type": "person_profile",
                     "images": [{"original_image_url": "…", "resized_image_url": "…",
                                 "watermarked_resized_image_url": "", "image_crops": {}}],
                     "videos": [], "cta_type": "LEARN_MORE", "page_id": 15087023444, "page_name": "Nike"},
        "totalActiveTime": 23488, "spend": None, "startDate": 1750316400, "publisherPlatform": ["facebook"],
        "url": "https://www.facebook.com/ads/library?id=1185617869915074",
        "startDateString": "2025-06-19T07:00:00.000Z", "endDateString": "2025-06-19T07:00:00.000Z",
        "aaa_info": None, "stats": EX_STATS},
    "transcript": ENVELOPE | {"data": {
        "ad_id": "1020359190509080", "url": "https://www.facebook.com/ads/library?id=1020359190509080",
        "transcript": "…", "transcript_available": True}},
    "health": {"status": "ok", "session_age_seconds": 120, "doc_ids_loaded": True},
}
ERROR_EXAMPLE = {"success": False, "error": "bad_request", "message": "Provide pageId or companyName"}
