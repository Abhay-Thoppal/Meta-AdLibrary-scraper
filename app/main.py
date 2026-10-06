"""FastAPI app: Meta Ad Library routes + /health. Swagger at /docs."""
import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from starlette.exceptions import HTTPException

from app import cache, service
from app.config import settings
from app.models import DELIVERY_NOTE, ERROR_EXAMPLE, EXAMPLES, CacheAge, CompanyAdsParams, SearchAdsParams
from app.scraper.graphql import MetaError
from app.scraper.session import session

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_):
    try:
        await session.ensure()
    except Exception as e:  # keep serving; the first request retries the bootstrap
        log.error("session bootstrap failed at startup: %s", e)
    yield
    await session.close()
    await cache.close()


_key = APIKeyHeader(name="x-api-key", auto_error=False)


def require_key(key: str | None = Security(_key)):
    if settings.api_key and key != settings.api_key:
        raise service.ApiError(401, "unauthorized", "Missing or invalid x-api-key")


app = FastAPI(title="Meta Ad Library API", version="0.1.0", lifespan=lifespan,
              description="Self-hosted REST API for the public Meta Ad Library: advertisers, ads, ad details with "
                          "EU/UK transparency, video transcripts and per-ad stats.\n\n" + DELIVERY_NOTE)
v1 = dict(dependencies=[Depends(require_key)], tags=["Facebook Ad Library"])


def err(status, error, message):
    return JSONResponse({"success": False, "error": error, "message": message}, status_code=status)


@app.exception_handler(service.ApiError)
async def _api_error(_: Request, e: service.ApiError):
    return err(e.status, e.error, e.message)


@app.exception_handler(MetaError)
async def _meta_error(_: Request, e: MetaError):
    return err(e.status, "rate_limited" if e.status == 429 else "scrape_failed", e.message)


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, e: RequestValidationError):
    msg = "; ".join(f"{'.'.join(str(x) for x in d['loc'][1:]) or 'body'}: {d['msg']}" for d in e.errors())
    return err(400, "bad_request", msg)


@app.exception_handler(HTTPException)
async def _http(_: Request, e: HTTPException):
    return err(e.status_code, "error", str(e.detail))


def docs(name, summary):
    errors = {s: {"description": d, "content": {"application/json": {"example": ERROR_EXAMPLE}}}
              for s, d in ((400, "Bad params"), (401, "Bad API key"), (404, "Not found"),
                           (429, "Meta rate-limited us"), (502, "Scrape failed"))}
    return {"summary": summary, "description": DELIVERY_NOTE,
            "responses": {200: {"content": {"application/json": {"example": EXAMPLES[name]}}}} | errors}


@app.get("/v1/facebook/adLibrary/search/companies", **v1, **docs("companies", "Search for Companies"))
async def search_companies(query: Annotated[str, Query(min_length=1, description="Company/page name keyword")]):
    return await service.search_companies(query)


@app.get("/v1/facebook/adLibrary/company/ads", **v1, **docs("company_ads", "Company Ads"))
async def company_ads_get(p: Annotated[CompanyAdsParams, Query()]):
    return await service.company_ads(p)


@app.post("/v1/facebook/adLibrary/company/ads", **v1, **docs("company_ads", "Company Ads (JSON body, for long cursors)"))
async def company_ads_post(p: CompanyAdsParams):
    return await service.company_ads(p)


@app.get("/v1/facebook/adLibrary/search/ads", **v1, **docs("search_ads", "Search Ads by keyword"))
async def search_ads_get(p: Annotated[SearchAdsParams, Query()]):
    return await service.search_ads(p)


@app.post("/v1/facebook/adLibrary/search/ads", **v1, **docs("search_ads", "Search Ads (JSON body, for long cursors)"))
async def search_ads_post(p: SearchAdsParams):
    return await service.search_ads(p)


Id = Annotated[str | None, Query(description="Ad archive id")]
Url = Annotated[str | None, Query(description="e.g. https://www.facebook.com/ads/library?id=1185617869915074")]
MaxAge = Annotated[CacheAge | None, Query(description="Serve from cache if the stored copy is at most this old")]


@app.get("/v1/facebook/adLibrary/ad", **v1, **docs("ad", "Ad Details"))
async def ad(id: Id = None, url: Url = None, trim: bool = False, cache_max_age: MaxAge = None):
    return await service.ad_details(service.ad_id_from(id, url), cache_max_age, trim)


@app.get("/v1/facebook/adLibrary/ad/transcript", **v1, **docs("transcript", "Ad Transcript"))
async def ad_transcript(id: Id = None, url: Url = None, cache_max_age: MaxAge = None):
    return await service.transcript(service.ad_id_from(id, url), cache_max_age)


@app.get("/health", tags=["Extras"], responses={200: {"content": {"application/json": {"example": EXAMPLES["health"]}}}})
async def health():
    age = session.age
    return {"status": "ok", "session_age_seconds": round(age) if age is not None else None,
            "doc_ids_loaded": bool(session.doc_ids)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
