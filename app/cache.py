"""§9 — single-table SQLite cache."""
import time

import aiosqlite
import orjson

from app.config import settings

_db: aiosqlite.Connection | None = None


async def db():
    global _db
    if _db is None:
        _db = await aiosqlite.connect(settings.cache_db_path)
        await _db.execute("CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, payload BLOB, created_at INTEGER)")
        await _db.commit()
    return _db


def key(endpoint, params):
    return endpoint + "?" + orjson.dumps({k: v for k, v in params.items() if v is not None}, option=orjson.OPT_SORT_KEYS).decode()


async def get(k, max_age):
    """Return (payload, created_at) if an entry is at most `max_age` seconds old, else None."""
    async with (await db()).execute("SELECT payload, created_at FROM cache WHERE key = ?", (k,)) as cur:
        row = await cur.fetchone()
    if row and time.time() - row[1] <= max_age:
        return orjson.loads(row[0]), row[1]
    return None


async def put(k, payload):
    d = await db()
    await d.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (k, orjson.dumps(payload), int(time.time())))
    await d.commit()


async def close():
    global _db
    if _db:
        await _db.close()
        _db = None
