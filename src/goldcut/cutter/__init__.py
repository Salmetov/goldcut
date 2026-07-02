"""Cutter — ffmpeg: вырез отрезка из кэшированного mp4, формат 9:16, вшитые субтитры.

Работает на сервере с полным файлом (fetcher.fetch_video) — нарезка локальная,
без повторных походов в YouTube. Субтитры генерируются из пословных таймкодов
(точных, из VTT), поэтому идеально попадают в речь.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from goldcut.models import Segment

# Стиль вшитых сабов (libass force_style)
SUB_STYLE = (
    "FontName=Helvetica,FontSize=14,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,"
    "Alignment=2,MarginV=80"
)
MAX_WORDS_PER_CAPTION = 4
CAPTION_GAP_S = 0.8          # пауза в речи → новая плашка


def _srt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def make_srt(seg: Segment, word_timings: list[tuple[float, str]]) -> str:
    """SRT для клипа: слова из [start_s, end_s], время — относительно начала клипа."""
    words = [(t - seg.start_s, w) for t, w in word_timings if seg.start_s <= t <= seg.end_s]
    if not words:
        return ""
    captions: list[list[tuple[float, str]]] = [[]]
    for t, w in words:
        cur = captions[-1]
        if cur and (len(cur) >= MAX_WORDS_PER_CAPTION or t - cur[-1][0] > CAPTION_GAP_S):
            captions.append([])
            cur = captions[-1]
        cur.append((t, w))

    clip_len = seg.end_s - seg.start_s
    out: list[str] = []
    for i, cap in enumerate(captions):
        start = cap[0][0]
        end = captions[i + 1][0][0] if i + 1 < len(captions) else min(cap[-1][0] + 1.2, clip_len)
        text = " ".join(w for _, w in cap)
        out.append(f"{i + 1}\n{_srt_ts(start)} --> {_srt_ts(end)}\n{text}\n")
    return "\n".join(out)


def cut_clip(
    source: str | Path,
    seg: Segment,
    out_path: str | Path,
    word_timings: list[tuple[float, str]] | None = None,
) -> Path:
    """Вырезать [start_s, end_s] из source → вертикальный 1080x1920 с сабами."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    vf = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=20:5[bg];"
        "[0:v]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[comp]"
    )

    with tempfile.TemporaryDirectory() as td:
        srt_text = make_srt(seg, word_timings or [])
        if srt_text:
            srt_path = Path(td) / "clip.srt"
            srt_path.write_text(srt_text, encoding="utf-8")
            vf += f";[comp]subtitles=clip.srt:force_style='{SUB_STYLE}'[v]"
        else:
            vf += ";[comp]null[v]"

        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{seg.start_s:.2f}", "-to", f"{seg.end_s:.2f}",
            "-i", str(Path(source).resolve()),
            "-filter_complex", vf,
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path.resolve()),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=td, timeout=600)
        if proc.returncode != 0 or not out_path.exists():
            raise RuntimeError(f"ffmpeg failed: {proc.stderr[-800:]}")
    return out_path
