"""Cutter — ffmpeg: вырез отрезка из кэшированного mp4 по RenderProfile.

Работает на сервере с полным файлом (fetcher.fetch_video) — нарезка локальная,
без повторных походов в YouTube. Субтитры — из точных пословных таймкодов (VTT/ASR).

Профили рендера:
  trim  — «как есть»: исходное соотношение (или кроп-заполнение до заданного),
          без блюр-фона. Быстрый, честная копия фрагмента (дефолт F1).
  short — вертикальный клип 9:16 с блюр-фоном (дефолт F2).
Сабы вшиваются, если profile.subtitles=True и есть пословные таймкоды.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from goldcut.models import RenderProfile

# Стиль вшитых сабов (libass force_style)
SUB_STYLE = (
    "FontName=Helvetica,FontSize=14,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,"
    "Alignment=2,MarginV=80"
)
MAX_WORDS_PER_CAPTION = 4
CAPTION_GAP_S = 0.8          # пауза в речи → новая плашка

# Целевые размеры по соотношению сторон.
ASPECT_DIMS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "16:9": (1920, 1080),
}


def _srt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def make_srt(start_s: float, end_s: float, word_timings: list[tuple[float, str]]) -> str:
    """SRT для клипа: слова из [start_s, end_s], время — относительно начала клипа."""
    words = [(t - start_s, w) for t, w in word_timings if start_s <= t <= end_s]
    if not words:
        return ""
    captions: list[list[tuple[float, str]]] = [[]]
    for t, w in words:
        cur = captions[-1]
        if cur and (len(cur) >= MAX_WORDS_PER_CAPTION or t - cur[-1][0] > CAPTION_GAP_S):
            captions.append([])
            cur = captions[-1]
        cur.append((t, w))

    clip_len = end_s - start_s
    out: list[str] = []
    for i, cap in enumerate(captions):
        start = cap[0][0]
        end = captions[i + 1][0][0] if i + 1 < len(captions) else min(cap[-1][0] + 1.2, clip_len)
        text = " ".join(w for _, w in cap)
        out.append(f"{i + 1}\n{_srt_ts(start)} --> {_srt_ts(end)}\n{text}\n")
    return "\n".join(out)


def _video_filter(profile: RenderProfile) -> str:
    """Собрать filter_complex до метки [comp] (без сабов)."""
    if profile.mode == "short":
        w, h = ASPECT_DIMS.get(profile.aspect_ratio) or ASPECT_DIMS["9:16"]
        return (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=20:5[bg];"
            f"[0:v]scale={w}:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[comp]"
        )
    # trim
    dims = ASPECT_DIMS.get(profile.aspect_ratio)
    if dims:                                   # кроп-заполнение до соотношения, без блюра
        w, h = dims
        return f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}[comp]"
    return "[0:v]null[comp]"                    # original — как есть


def render(
    source: str | Path,
    start_s: float,
    end_s: float,
    out_path: str | Path,
    profile: RenderProfile,
    word_timings: list[tuple[float, str]] | None = None,
) -> Path:
    """Вырезать [start_s, end_s] из source по профилю → mp4."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    vf = _video_filter(profile)
    with tempfile.TemporaryDirectory() as td:
        if profile.subtitles and word_timings:
            srt_text = make_srt(start_s, end_s, word_timings)
            if srt_text:
                (Path(td) / "clip.srt").write_text(srt_text, encoding="utf-8")
                vf += f";[comp]subtitles=clip.srt:force_style='{SUB_STYLE}'[v]"
            else:
                vf += ";[comp]null[v]"
        else:
            vf += ";[comp]null[v]"

        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start_s:.2f}", "-to", f"{end_s:.2f}",
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


# ── обратная совместимость: старый bot зовёт cut_clip(source, seg, out, wt) ──
def cut_clip(source, seg, out_path, word_timings=None):
    """DEPRECATED-шов: рендер по старому контракту (9:16 + сабы). Уйдёт с переписью бота."""
    return render(
        source, seg.start_s, seg.end_s, out_path,
        RenderProfile(mode="short", aspect_ratio="9:16", subtitles=True),
        word_timings,
    )
