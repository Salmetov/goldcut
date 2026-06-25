"""VTT → таймкодированный транскрипт и хелперы YouTube heatmap.

Авто-субтитры YouTube приходят с пословными тегами и дублированием строк
(каждая строка повторяется при «прокрутке»). Здесь это чистится.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

_TS = re.compile(r"(\d+):(\d+):(\d+)\.(\d+)\s+-->")
_TAG = re.compile(r"<[^>]+>")


def mmss(t: float) -> str:
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def parse_vtt(path: str | Path, bucket_s: int = 20) -> str:
    """VTT → строки вида '[MM:SS] текст', сгруппированные по ~bucket_s секунд."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    pairs: list[tuple[float, str]] = []
    cur: float | None = None
    last: str | None = None
    for line in lines:
        m = _TS.match(line)
        if m:
            h, mm, s, ms = m.groups()
            cur = int(h) * 3600 + int(mm) * 60 + int(s) + int(ms) / 1000
            continue
        if cur is None or "-->" in line:
            continue
        text = html.unescape(_TAG.sub("", line)).strip()
        if text and text != last:            # дедуп прокрутки авто-сабов
            pairs.append((cur, text))
            last = text

    out: list[str] = []
    buf: list[str] = []
    start: float | None = None
    for t, txt in pairs:
        if start is None:
            start = t
        buf.append(txt)
        if t - start >= bucket_s:
            out.append(f"[{mmss(start)}] " + " ".join(buf))
            buf, start = [], None
    if buf and start is not None:
        out.append(f"[{mmss(start)}] " + " ".join(buf))
    return "\n".join(out)


def load_heatmap(info_json_path: str | Path) -> list[tuple[float, float]]:
    """Достаёт «самые пересматриваемые» точки из yt-dlp info.json."""
    info = json.loads(Path(info_json_path).read_text(encoding="utf-8"))
    hm = info.get("heatmap") or []
    return [(float(p["start_time"]), float(p["value"])) for p in hm]


def heatmap_peaks(
    heatmap: list[tuple[float, float]], top: int = 15
) -> list[tuple[float, float]]:
    return sorted(heatmap, key=lambda x: x[1], reverse=True)[:top]
