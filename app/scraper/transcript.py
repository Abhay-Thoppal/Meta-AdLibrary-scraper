"""§10.6 — download the ad video, ffmpeg -> 16k mono wav, faster-whisper. Optional extra."""
import asyncio
import logging
import os
import subprocess
import tempfile
from functools import lru_cache

from app.config import settings
from app.scraper.graphql import client

log = logging.getLogger(__name__)


def pick_video(snapshot):
    """First playable video URL: videos (sd before hd), then extra_videos, then cards."""
    pools = [snapshot.get("videos") or [], snapshot.get("extra_videos") or [], snapshot.get("cards") or []]
    for pool in pools:
        for v in pool:
            for k in ("video_sd_url", "video_hd_url", "video_sd_handle", "video_hd_handle"):
                if (v.get(k) or "").startswith("http"):
                    return v[k]
    return None


@lru_cache(maxsize=1)
def _model():
    from faster_whisper import WhisperModel
    return WhisperModel(settings.whisper_model, device=settings.whisper_device)


def _transcribe(path):
    """ffmpeg decodes to 16k mono PCM on stdout -> whisper. Skips faster-whisper's PyAV decoder entirely."""
    import numpy as np
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                       capture_output=True, timeout=300)
    if r.returncode:
        err = r.stderr.decode(errors="replace")
        if "does not contain any stream" in err or "matches no streams" in err:
            return ""  # silent video: no speech
        raise RuntimeError(f"ffmpeg: {err.strip()[:200]}")
    audio = np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768
    segments, _ = _model().transcribe(audio, vad_filter=True)  # language auto-detect (Hinglish ads etc.)
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe_url(url):
    """Transcript text; "" = no speech (cacheable); None = couldn't try (no faster-whisper / download failed)."""
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        log.warning("faster-whisper not installed: `uv sync --extra transcribe` to enable transcripts")
        return None
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        cap, size = settings.max_video_mb * 1024 * 1024, 0
        async with client().stream("GET", url) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > cap:
                        raise ValueError(f"video larger than MAX_VIDEO_MB={settings.max_video_mb}")
                    f.write(chunk)
        return await asyncio.to_thread(_transcribe, path)
    except Exception as e:
        log.warning("transcription failed for %s: %s", url[:80], e)
        return None
    finally:
        os.remove(path)
