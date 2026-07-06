"""LocalFetcher — добыча прямо на сервере (yt-dlp локально), без Mac/Tailscale.

Тот же контракт, что TailscaleFetcher (meta / fetch_video / health), но yt-dlp
запускается как подпроцесс на сервере. Работает, пока YouTube не блокирует IP
сервера (проверено — не блокирует). Если однажды заблокирует — за швом Fetcher
можно вернуть резидентный бэкенд, не трогая остальной код.

Запросы к YouTube идут НАПРЯМУЮ (proxy из окружения снимается) + через deno
(JS-рантайм для n-challenge → полноценные форматы).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from goldcut.fetcher import youtube_id
from goldcut.models import VideoMeta
from goldcut.transcript import parse_vtt_text, parse_vtt_words_text

log = logging.getLogger(__name__)

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")


class LocalFetcher:
    def __init__(
        self,
        cache_dir: str | Path = "cache",
        sub_langs: str = "en-orig,en,ru-orig,ru",
        *,
        ytdlp: str = "yt-dlp",
        deno: str | None = None,
        extractor_args: str = "youtube:player_client=android,ios,tv",
        video_format: str = "b[ext=mp4]/b",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.sub_langs = sub_langs
        self.ytdlp = ytdlp
        self.deno = deno
        self.extractor_args = extractor_args
        self.video_format = video_format

    # ── запуск yt-dlp: без прокси, с deno ──
    def _env(self) -> dict:
        e = dict(os.environ)
        for k in _PROXY_VARS:
            e.pop(k, None)
        if self.deno:
            e["PATH"] = str(Path(self.deno).parent) + ":" + e.get("PATH", "")
        return e

    def _run(self, args: list[str], timeout: int) -> subprocess.CompletedProcess:
        cmd = [self.ytdlp]
        if self.extractor_args:
            cmd += ["--extractor-args", self.extractor_args]
        if self.deno:
            cmd += ["--js-runtimes", f"deno:{self.deno}"]
        cmd += args
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=self._env()
        )

    def health(self) -> dict:
        p = self._run(["--version"], 20)
        return {"ok": p.returncode == 0, "ytdlp": p.stdout.strip()[:20], "backend": "local"}

    # ── стадия A: субтитры + heatmap (текст) ──
    def meta(self, url: str, sub_langs: str | None = None, *, force: bool = False) -> VideoMeta:
        vid = youtube_id(url)
        cache = self.cache_dir / f"{vid}.meta.json" if vid else None
        if cache and cache.exists() and not force:
            return VideoMeta.model_validate_json(cache.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as td:
            p = self._run(
                [
                    "--skip-download", "--write-auto-sub", "--write-sub",
                    "--sub-langs", sub_langs or self.sub_langs, "--sub-format", "vtt",
                    "--write-info-json", "-o", os.path.join(td, "vid.%(ext)s"), url,
                ],
                300,
            )
            info_files = glob.glob(os.path.join(td, "*.info.json"))
            if not info_files:
                raise RuntimeError(f"yt-dlp meta failed: {p.stderr[-800:]}")
            info = json.loads(Path(info_files[0]).read_text(encoding="utf-8"))
            # предпочитаем не-orig дорожку (как воркер) — проверенное поведение
            vtts = sorted(glob.glob(os.path.join(td, "*.vtt")), key=lambda x: "orig" in x)
            vtt = Path(vtts[0]).read_text(encoding="utf-8") if vtts else ""

        if not vtt:
            raise RuntimeError("fetcher meta: субтитры не найдены (vtt пуст)")
        meta = VideoMeta(
            url=url,
            title=info.get("title", ""),
            duration_s=float(info.get("duration") or 0),
            transcript=parse_vtt_text(vtt),
            heatmap=[(float(h["start_time"]), float(h["value"])) for h in (info.get("heatmap") or [])],
            word_timings=parse_vtt_words_text(vtt),
        )
        if cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(meta.model_dump_json(), encoding="utf-8")
        return meta

    # ── стадия B: полный mp4 в кэш (один раз на видео) ──
    def fetch_video(self, url: str) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        vid = youtube_id(url)
        if not vid:
            raise RuntimeError("fetch_video: не смог извлечь video_id")
        local = self.cache_dir / f"{vid}.mp4"
        if local.exists() and local.stat().st_size > 0:
            return local
        p = self._run(
            ["-f", self.video_format, "--no-playlist", "--merge-output-format", "mp4",
             "-o", str(local), url],
            1800,
        )
        if not local.exists():
            raise RuntimeError(f"yt-dlp download failed: {p.stderr[-800:]}")
        return local
