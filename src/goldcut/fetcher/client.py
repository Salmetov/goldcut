"""Серверная сторона Fetcher: дёргает воркер на Mac по tailnet.

Реализует протокол `goldcut.fetcher.Fetcher`. Никакой логики yt-dlp здесь нет —
вся добыча происходит на Mac (резидентный IP). Тут только HTTP-вызовы по Tailscale.

Основной поток видео: POST /download (Mac кэширует полный mp4) → GET /video/{id}
(сервер забирает файл и режет у себя ffmpeg-ом) — одна закачка с YouTube на видео,
дальше любые нарезки локально.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from goldcut.fetcher import youtube_id
from goldcut.models import VideoMeta
from goldcut.transcript import parse_vtt_text, parse_vtt_words_text


class TailscaleFetcher:
    """Fetcher поверх HTTP-воркера на Mac, доступного только в tailnet."""

    def __init__(self, base_url: str, cache_dir: str | Path = "cache") -> None:
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir)

    # Fetcher ходит ТОЛЬКО в tailnet (на Mac) — прокси из окружения тут вреден
    # (перехватывает запрос и отдаёт 405). trust_env=False = всегда напрямую.
    def health(self) -> dict:
        with httpx.Client(timeout=10, trust_env=False) as c:
            return c.get(f"{self.base_url}/health").json()

    def meta(self, url: str, sub_langs: str = "en.*", *, force: bool = False) -> VideoMeta:
        """Стадия A: субтитры + heatmap (килобайты). Видео не скачивается.

        Результат кэшируется на диске по video_id: субтитры готового ролика не
        меняются, поэтому повторная ссылка не ходит на Mac/YouTube вообще.
        """
        vid = youtube_id(url)
        cache = self.cache_dir / f"{vid}.meta.json" if vid else None
        if cache and cache.exists() and not force:
            return VideoMeta.model_validate_json(cache.read_text(encoding="utf-8"))
        meta = self._meta_fetch(url, sub_langs)
        if cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(meta.model_dump_json(), encoding="utf-8")
        return meta

    def _meta_fetch(self, url: str, sub_langs: str) -> VideoMeta:
        with httpx.Client(timeout=300, trust_env=False) as c:
            r = c.post(f"{self.base_url}/meta", json={"url": url, "sub_langs": sub_langs})
            r.raise_for_status()
            data = r.json()
        if data.get("error"):
            raise RuntimeError(f"fetcher meta: {data['error']}")
        vtt = data.get("vtt", "")
        if not vtt:
            raise RuntimeError("fetcher meta: субтитры не найдены (vtt пуст)")
        return VideoMeta(
            url=url,
            title=data["title"],
            duration_s=data["duration_s"],
            transcript=parse_vtt_text(vtt),
            heatmap=[tuple(p) for p in data.get("heatmap", [])],
            word_timings=parse_vtt_words_text(vtt),
        )

    def fetch_video(self, url: str) -> Path:
        """Стадия B: полный mp4 через кэш Mac → локальный кэш сервера."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=1800, trust_env=False) as c:
            r = c.post(f"{self.base_url}/download", json={"url": url})
            r.raise_for_status()
            info = r.json()
            if info.get("error"):
                raise RuntimeError(f"fetcher download: {info['error']}")
            video_id, size = info["video_id"], info["size"]

            local = self.cache_dir / f"{video_id}.mp4"
            if local.exists() and local.stat().st_size == size:
                return local

            tmp = local.with_suffix(".part")
            with c.stream("GET", f"{self.base_url}/video/{video_id}") as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(1 << 20):
                        f.write(chunk)
            tmp.rename(local)
        return local
