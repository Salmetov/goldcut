"""VTT → таймкодированный транскрипт и хелперы YouTube heatmap.

Авто-субтитры YouTube приходят с пословными тегами и дублированием строк
(каждая строка повторяется при «прокрутке»). Здесь это чистится.
"""

from __future__ import annotations

import bisect
import difflib
import html
import json
import re
from pathlib import Path

_TS = re.compile(r"(\d+):(\d+):(\d+)\.(\d+)\s+-->")
_TAG = re.compile(r"<[^>]+>")
_WORDTS = re.compile(r"<(\d+):(\d+):(\d+)\.(\d+)>")
_NONWORD = re.compile(r"[^a-z0-9]+")


def _to_s(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _norm(word: str) -> str:
    return _NONWORD.sub("", word.lower())


def mmss(t: float) -> str:
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def parse_vtt(path: str | Path, bucket_s: int = 20) -> str:
    """VTT-файл → строки вида '[MM:SS] текст' (см. parse_vtt_text)."""
    return parse_vtt_text(Path(path).read_text(encoding="utf-8"), bucket_s)


def parse_vtt_text(vtt_text: str, bucket_s: int = 20) -> str:
    """VTT → строки вида '[MM:SS] текст', сгруппированные по ~bucket_s секунд."""
    lines = vtt_text.splitlines()
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


def parse_vtt_words(path: str | Path) -> list[tuple[float, str]]:
    """VTT-файл → пословный поток (см. parse_vtt_words_text)."""
    return parse_vtt_words_text(Path(path).read_text(encoding="utf-8"))


def parse_vtt_words_text(vtt_text: str) -> list[tuple[float, str]]:
    """Пословный поток (секунды, слово) из таймкодов авто-сабов YouTube.

    Берём только строки с пословными тегами <ts>; первое слово до первого тега
    получает время реплики, остальные — свой <ts>. Не-таймкодированные строки
    (дубли «прокрутки») пропускаются → естественный дедуп.
    """
    lines = vtt_text.splitlines()
    words: list[tuple[float, str]] = []
    cue_start: float | None = None
    for line in lines:
        m = _TS.match(line)
        if m:
            cue_start = _to_s(*m.groups())
            continue
        if "-->" in line or "<" not in line:
            continue
        first = _WORDTS.search(line)
        if not first:
            continue
        if cue_start is not None:
            lead = html.unescape(line[: first.start()].replace("<c>", "").replace("</c>", ""))
            for w in lead.split():
                words.append((cue_start, w))
        for mt in re.finditer(
            r"<(\d+):(\d+):(\d+)\.(\d+)>(.*?)(?=<\d+:\d+:\d+\.\d+>|$)", line
        ):
            ts = _to_s(*mt.group(1, 2, 3, 4))
            seg = html.unescape(mt.group(5).replace("<c>", "").replace("</c>", ""))
            for w in seg.split():
                words.append((ts, w))
    return words


def snap_timecodes(
    words: list[tuple[float, str]],
    clip_text: str,
    pad_end: float = 0.4,
) -> tuple[float, float] | None:
    """Выровнять текст клипа по пословному потоку → точные (start_s, end_s).

    Якоримся на САМОМ длинном совпадающем блоке (он попадает в нужный регион) и
    проецируем границы клипа от него — так короткие ложные совпадения вроде
    «if there was a» в начале ролика не растягивают клип. Возвращает None при
    слабом/отсутствующем выравнивании.
    """
    timed = [(t, _norm(w)) for t, w in words]
    timed = [(t, n) for t, n in timed if n]
    if not timed:
        return None
    seq = [n for _, n in timed]
    clip = [n for n in (_norm(w) for w in clip_text.split()) if n]
    if not clip:
        return None

    sm = difflib.SequenceMatcher(None, seq, clip, autojunk=False)
    lm = sm.find_longest_match(0, len(seq), 0, len(clip))
    if lm.size < 4:                                   # нет уверенного якоря
        return None

    start_idx = max(0, lm.a - lm.b)                   # проекция: clip[0] ≈ здесь
    end_idx = min(len(timed) - 1, start_idx + len(clip) - 1)
    return timed[start_idx][0], timed[end_idx][0] + pad_end


_SENT_END = re.compile(r"[.!?…][\"')\]]*$")


def build_sentences(
    words: list[tuple[float, str]],
    *,
    pause_s: float = 1.2,
    max_words: int = 45,
    tail_pad_s: float = 0.6,
) -> list[tuple[float, float, str]]:
    """Пословный поток → список предложений (start_s, end_s, text).

    Границы: пунктуация конца предложения в авто-сабах, длинная пауза в речи,
    либо принудительный разрез после max_words (защита от кусков без пунктуации).
    end_s предложения = начало следующего слова (полный хвост последнего слова).
    Это канонические единицы для выбора клипов: LLM выбирает диапазон предложений,
    код берёт их точные таймкоды — никакой привязки текста не нужно.
    """
    sentences: list[tuple[float, float, str]] = []
    cur: list[tuple[float, str]] = []
    for k, (t, w) in enumerate(words):
        cur.append((t, w))
        next_t = words[k + 1][0] if k + 1 < len(words) else None
        boundary = (
            _SENT_END.search(w)
            or (next_t is not None and next_t - t > pause_s)
            or len(cur) >= max_words
        )
        if boundary:
            end_s = next_t if next_t is not None else t + tail_pad_s
            sentences.append((cur[0][0], end_s, " ".join(x for _, x in cur)))
            cur = []
    if cur:
        sentences.append((cur[0][0], cur[-1][0] + tail_pad_s, " ".join(x for _, x in cur)))
    return sentences


def extend_to_sentence_bounds(
    words: list[tuple[float, str]],
    start_s: float,
    end_s: float,
    *,
    max_back_s: float = 8.0,
    max_fwd_s: float = 5.0,
    pause_s: float = 1.2,
    pad_end: float = 0.4,
) -> tuple[float, float]:
    """Расширить [start_s, end_s] до границ предложений — чтобы мысль была целой.

    LLM/привязка могут дать клип, начинающийся с середины фразы (например, из-за
    разбиения транскрипта на ~20с-абзацы). Идём назад от начала клипа, пока
    предыдущее слово не завершает предложение (пунктуация `.?!` в авто-сабах) или
    не встретилась пауза в речи > pause_s; аналогично вперёд до конца предложения.
    Расширение ограничено max_back_s / max_fwd_s.
    """
    if not words:
        return start_s, end_s
    times = [t for t, _ in words]

    i = min(bisect.bisect_left(times, start_s), len(words) - 1)
    while i > 0:
        prev_t, prev_w = words[i - 1]
        if _SENT_END.search(prev_w):          # предыдущее слово закончило предложение
            break
        if start_s - prev_t > max_back_s:     # не уходим слишком далеко назад
            break
        if words[i][0] - prev_t > pause_s:    # пауза в речи ≈ граница мысли
            break
        i -= 1

    j = max(bisect.bisect_right(times, end_s) - 1, 0)
    while j < len(words) - 1:
        if _SENT_END.search(words[j][1]):     # текущее слово завершает предложение
            break
        next_t = words[j + 1][0]
        if next_t - end_s > max_fwd_s or next_t - words[j][0] > pause_s:
            break
        j += 1

    return words[i][0], words[j][0] + pad_end
