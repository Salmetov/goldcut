#!/usr/bin/env python3
"""Воркер добычи — крутится НА Mac, слушает только Tailscale-интерфейс.

Самодостаточный файл: ТОЛЬКО stdlib (на Mac нет pip/brew), оборачивает yt-dlp.
Сервер (fetcher.client) обращается сюда по tailnet; наружу воркер не выставляется.

Эндпоинты:
  GET  /health          → {ok, ytdlp, ffmpeg}
  POST /meta {url}      → {title, duration_s, vtt, heatmap}   (только текст, килобайты)
  POST /download {url}  → {video_id, size} — скачать полный mp4 в кэш (один раз на видео)
  GET  /video/{id}      → стрим кэшированного mp4 (сервер режет у себя)
  POST /cut  {url, sections:[{start_s,end_s}]} → {files:[{name, b64}]}  (нужен ffmpeg на Mac;
             опциональный эффективный путь — основной поток идёт через /download)

Деплой (launchd, авто-старт): см. docs/decisions/0002-*.md. Ручной запуск:
    python3 worker.py --host 100.119.65.77 --port 8765
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import shutil
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

YTDLP = os.path.expanduser("~/bin/yt-dlp")
FFMPEG = os.path.expanduser("~/bin/ffmpeg")
CACHE_DIR = os.path.expanduser("~/goldcut-worker/cache")
# Мобильные клиенты отдают субтитры/форматы без JS-челленджа (deno не нужен)
EXTRACTOR_ARGS = "youtube:player_client=android,ios,tv"
TIMEOUT_META_S = 240
TIMEOUT_CUT_S = 900
TIMEOUT_DL_S = 1800


def _ffmpeg_path() -> str | None:
    if os.path.exists(FFMPEG):
        return FFMPEG
    return shutil.which("ffmpeg")


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    ff = _ffmpeg_path()
    if ff:  # yt-dlp находит ffmpeg через PATH
        env["PATH"] = os.path.dirname(ff) + os.pathsep + env.get("PATH", "")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def do_meta(url: str, sub_langs: str = "en.*") -> dict:
    with tempfile.TemporaryDirectory() as td:
        proc = _run(
            [
                YTDLP, "--skip-download", "--write-auto-sub", "--write-sub",
                "--sub-langs", sub_langs, "--sub-format", "vtt",
                "--write-info-json", "-o", os.path.join(td, "vid.%(ext)s"),
                "--extractor-args", EXTRACTOR_ARGS, url,
            ],
            TIMEOUT_META_S,
        )
        info_files = glob.glob(os.path.join(td, "*.info.json"))
        if not info_files:
            raise RuntimeError(f"yt-dlp meta failed: {proc.stderr[-800:]}")
        info = json.load(open(info_files[0], encoding="utf-8"))

        # предпочитаем не-orig дорожку (обычная en), иначе первую попавшуюся
        vtts = sorted(glob.glob(os.path.join(td, "*.vtt")), key=lambda p: "orig" in p)
        vtt_text = open(vtts[0], encoding="utf-8").read() if vtts else ""

        return {
            "url": url,
            "title": info.get("title", ""),
            "duration_s": float(info.get("duration") or 0),
            "vtt": vtt_text,
            "heatmap": [
                [float(p["start_time"]), float(p["value"])]
                for p in (info.get("heatmap") or [])
            ],
        }


def do_download(url: str) -> dict:
    """Скачать полный mp4 в кэш (идемпотентно). Сервер потом заберёт через GET /video/{id}."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    proc = _run(
        [YTDLP, "--print", "id", "--skip-download",
         "--extractor-args", EXTRACTOR_ARGS, url],
        TIMEOUT_META_S,
    )
    video_id = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if not video_id:
        raise RuntimeError(f"yt-dlp id failed: {proc.stderr[-500:]}")
    path = os.path.join(CACHE_DIR, f"{video_id}.mp4")
    if not os.path.exists(path):
        proc = _run(
            [YTDLP, "-f", "b[ext=mp4]/b", "--no-playlist",
             "-o", path, "--extractor-args", EXTRACTOR_ARGS, url],
            TIMEOUT_DL_S,
        )
        if not os.path.exists(path):
            raise RuntimeError(f"yt-dlp download failed: {proc.stderr[-800:]}")
    return {"video_id": video_id, "size": os.path.getsize(path)}


def do_cut(url: str, sections: list[dict]) -> dict:
    if not _ffmpeg_path():
        raise RuntimeError("ffmpeg_missing: установите ffmpeg в ~/bin (см. ADR 0002)")
    out: list[dict] = []
    with tempfile.TemporaryDirectory() as td:
        for i, sec in enumerate(sections):
            start, end = float(sec["start_s"]), float(sec["end_s"])
            tmpl = os.path.join(td, f"clip{i:02d}.%(ext)s")
            proc = _run(
                [
                    YTDLP, "-f", "b[ext=mp4]/b", "--no-playlist",
                    "--download-sections", f"*{start:.2f}-{end:.2f}",
                    "--force-keyframes-at-cuts",
                    "-o", tmpl, "--extractor-args", EXTRACTOR_ARGS, url,
                ],
                TIMEOUT_CUT_S,
            )
            files = glob.glob(os.path.join(td, f"clip{i:02d}.*"))
            if not files:
                raise RuntimeError(
                    f"yt-dlp cut failed (section {i}): {proc.stderr[-800:]}"
                )
            data = open(files[0], "rb").read()
            out.append(
                {
                    "name": os.path.basename(files[0]),
                    "start_s": start,
                    "end_s": end,
                    "b64": base64.b64encode(data).decode("ascii"),
                }
            )
    return {"files": out}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(
                200,
                {
                    "ok": True,
                    "ytdlp": os.path.exists(YTDLP),
                    "ffmpeg": bool(_ffmpeg_path()),
                },
            )
            return
        if self.path.startswith("/video/"):
            video_id = os.path.basename(self.path[len("/video/"):])
            path = os.path.join(CACHE_DIR, f"{video_id}.mp4")
            if not os.path.exists(path):
                self._send(404, {"error": "not_cached"})
                return
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/meta":
                self._send(200, do_meta(req["url"], req.get("sub_langs", "en.*")))
            elif self.path == "/download":
                self._send(200, do_download(req["url"]))
            elif self.path == "/cut":
                self._send(200, do_cut(req["url"], req["sections"]))
            else:
                self._send(404, {"error": "not_found"})
        except Exception as exc:  # ошибки — в JSON, воркер не падает
            self._send(500, {"error": str(exc)[:2000]})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[worker] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="100.119.65.77")  # tailnet-IP Mac, не 0.0.0.0
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[worker] listening on {args.host}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
