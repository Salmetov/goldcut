"""Retrieval — локализатор фрагмента по запросу (ядро F1).

«Дай кусок на 12-й минуте про X»:
  1. время-ориентир — МЯГКИЙ приор: окно ±WINDOW_S вокруг него (человек ошибается).
  2. есть тема → LLM выбирает диапазон предложений в окне (паттерн «LLM выбирает,
     не генерирует» — границы не дрейфуют) + confidence.
  3. только время → берём мысль вокруг ориентира детерминированно.
  4. низкий confidence → возвращаем как есть, решение переспросить — на слое диалога.

Предложения строит transcript.build_sentences (уже целые мысли по построению).
"""

from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel

from goldcut.config import Config, anthropic_client
from goldcut.models import Candidate, Request, VideoMeta
from goldcut.transcript import build_sentences, mmss

log = logging.getLogger(__name__)

LOCATOR_VERSION = "v1"

WINDOW_S = 120.0          # ± окно вокруг времени-ориентира
DEFAULT_CLIP_S = 45.0     # целевая длина, когда названо только время
MIN_CLIP_S, MAX_CLIP_S = 6.0, 300.0


class _Located(BaseModel):
    found: bool
    first_sentence: int = 0
    last_sentence: int = 0
    title: str = ""
    confidence: float = 0.0   # 0..1


SYSTEM = """\
Тебе дают фрагмент транскрипта видео — ПРОНУМЕРОВАННЫЕ предложения вида \
«S123 [MM:SS] текст» — и ЗАПРОС пользователя, какой момент он хочет вырезать. \
Найди диапазон предложений (first_sentence..last_sentence, включительно), который \
ТОЧНО и ЦЕЛИКОМ отвечает запросу.

Правила:
- Диапазон — это цельная, законченная мысль по теме запроса; читается понятно без \
остального видео. Включи предложения-контекст, если ключевая фраза опирается на них.
- Начинай с сильного, осмысленного предложения (не с «So», «And» без содержания).
- Если подходящего момента в этом фрагменте НЕТ — верни found=false.
- confidence: 0..1 — насколько уверенно диапазон соответствует именно запросу \
(1.0 — точное попадание; <0.5 — сомнительно/приблизительно).

Верни first_sentence, last_sentence, title (короткий заголовок момента), confidence.\
"""


def _window_indices(sentences, anchor: float | None) -> tuple[int, int]:
    if anchor is None:
        return 0, len(sentences) - 1
    lo = anchor - WINDOW_S
    hi = anchor + WINDOW_S
    idx = [i for i, (s, e, _t) in enumerate(sentences) if e >= lo and s <= hi]
    if not idx:
        # ориентир за пределами — берём ближайшее предложение
        j = min(range(len(sentences)), key=lambda i: abs(sentences[i][0] - anchor))
        return j, j
    return idx[0], idx[-1]


def _around_anchor(sentences, anchor: float, target_s: float) -> Candidate:
    """Только время: собрать мысль вокруг ориентира до ~target_s, снапнув на предложения."""
    start_i = min(range(len(sentences)), key=lambda i: abs(sentences[i][0] - anchor))
    j = start_i
    while j < len(sentences) - 1 and sentences[j][1] - sentences[start_i][0] < target_s:
        j += 1
    start_s, end_s = sentences[start_i][0], sentences[j][1]
    text = " ".join(t for _s, _e, t in sentences[start_i : j + 1])
    return Candidate(start_s=round(start_s, 2), end_s=round(end_s, 2),
                     transcript=text, title=f"Фрагмент с {mmss(start_s)}", confidence=0.6)


def locate(
    meta: VideoMeta,
    request: Request,
    *,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
    cfg: Config | None = None,
) -> list[Candidate]:
    """Вернуть кандидатов на фрагмент (обычно 1) под запрос F1."""
    cfg = cfg or Config.from_env()
    sentences = build_sentences(meta.word_timings)
    if not sentences:
        raise RuntimeError("locate: нет пословных таймкодов")

    lo, hi = _window_indices(sentences, request.time_anchor_s)

    # только время, без темы → детерминированно вокруг ориентира
    if not request.topic and request.time_anchor_s is not None:
        target = request.length_pref_s or DEFAULT_CLIP_S
        return [_around_anchor(sentences, request.time_anchor_s, target)]

    # есть тема → LLM выбирает диапазон в окне
    client = client or anthropic_client(cfg)
    window = [(i, sentences[i]) for i in range(lo, hi + 1)]
    sent_lines = "\n".join(f"S{i} [{mmss(s)}] {t}" for i, (s, _e, t) in window)
    query = request.topic or request.raw
    user = (
        f"РОЛИК: {meta.title}\n\nЗАПРОС ПОЛЬЗОВАТЕЛЯ: {query}\n\n"
        f"ФРАГМЕНТ ТРАНСКРИПТА:\n{sent_lines}"
    )
    resp = client.messages.parse(
        model=model or cfg.analyzer_model,
        max_tokens=800,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=_Located,
    )
    r = resp.parsed_output
    if not r.found:
        return []

    i = max(lo, min(r.first_sentence, hi))
    j = max(i, min(r.last_sentence, hi))
    start_s, end_s = sentences[i][0], sentences[j][1]
    dur = end_s - start_s
    if not (MIN_CLIP_S <= dur <= MAX_CLIP_S):
        log.warning("locate: диапазон S%s–S%s = %.0fs вне пределов", i, j, dur)
        # мягко подрежем/расширим до предела вместо отбрасывания
        end_s = start_s + min(max(dur, MIN_CLIP_S), MAX_CLIP_S)
    text = " ".join(t for _s, _e, t in sentences[i : j + 1])
    return [Candidate(start_s=round(start_s, 2), end_s=round(end_s, 2),
                      transcript=text, title=r.title or "Найденный фрагмент",
                      confidence=round(r.confidence, 2))]
