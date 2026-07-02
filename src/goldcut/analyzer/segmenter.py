"""Analyzer — сердце системы: отбор «золота» из транскрипта.

Реализует метод из docs/analyzer-method.md. Ключевой принцип: LLM ВЫБИРАЕТ,
а не генерирует. Код строит из пословных таймкодов пронумерованные предложения
с точным временем; LLM возвращает клип как диапазон предложений (S412–S418).
Таймкоды и дословный текст берутся из диапазона — привязка текста, снапинг и
починка границ не нужны по построению. Скоринг (веса, heatmap-буст) — в коде.
"""

from __future__ import annotations

import logging

import anthropic

import re

from goldcut.config import Config
from goldcut.models import Segment, SegmentSelection, VideoMeta
from goldcut.transcript import build_sentences, heatmap_peaks, mmss

log = logging.getLogger(__name__)

_ENDS_CLEAN = re.compile(r"[.!?…][\"')\]]*$")

# Насколько можно авто-дотянуть конец диапазона до конца фразы
MAX_TAIL_EXTEND_SENTENCES = 2
MAX_TAIL_EXTEND_S = 6.0

# Веса рубрики для итогового балла. Тюнятся по результатам прогонов.
WEIGHTS = {
    "self_contained": 0.25,
    "hook": 0.20,
    "insight": 0.25,
    "emotion": 0.15,
    "payoff": 0.15,
}

# Во сколько раз пик heatmap (0..1) добавляется к итоговому баллу.
HEATMAP_SCALE = 0.5

# Допустимая длительность клипа (сек); вне пределов — кандидат отбрасывается.
MIN_CLIP_S, MAX_CLIP_S = 10.0, 90.0

SYSTEM = """\
Ты — редактор коротких вертикальных видео (TikTok/Shorts). Тебе дают транскрипт \
длинного ролика, разбитый на ПРОНУМЕРОВАННЫЕ ПРЕДЛОЖЕНИЯ вида «S123 [MM:SS] текст». \
Твоя задача — НЕ нарезать ролик подряд, а ВЫУДИТЬ ЗОЛОТО: выбрать отдельные \
самодостаточные моменты, где проговаривается одна ясная, неочевидная или эмоционально \
цепляющая мысль, которая работает как клип сама по себе.

Каждый клип ты задаёшь ДИАПАЗОНОМ предложений: first_sentence и last_sentence \
(включительно). В клип попадёт дословно всё от начала первого до конца последнего \
предложения диапазона — выбирай так, чтобы этот текст читался как цельное, законченное \
высказывание.

Требования к диапазону:
- 15–60 секунд речи (ориентируйся по меткам [MM:SS] первого и последнего предложения).
- МЫСЛЬ ЦЕЛИКОМ: если ключевая фраза опирается на контекст («this», «that experiment», \
«it» из предыдущей фразы, ответ на вопрос ведущего) — ВКЛЮЧИ предложения с этим \
контекстом в диапазон. Зритель видит только клип: перечитай текст диапазона глазами \
человека, который не смотрел ролик.
- Первое предложение диапазона — сильный хук; последнее — развязка. Не начинай с \
вялых подводок («So», «And», «Yeah» без содержания) — сдвинь диапазон.
- Некоторые «предложения» оборваны паузой в речи и не заканчиваются точкой — НЕ \
заканчивай диапазон таким предложением, включи продолжение до конца фразы.
- Большая часть ролика не должна дать ничего — это нормально. Качество важнее количества.
- НЕ включай: рекламные вставки и спонсорские чтения, само-промо канала (подписка, \
промокоды), вступительный трейлер-нарезку ролика, организационную болтовню, повторы.
- Ищи разнообразие: разные мысли из разных мест ролика.

Тебе также дают «самые пересматриваемые» моменты (heatmap) — сигнал, где зрители \
залипают. Учитывай, но суди самостоятельно.

Для каждого клипа заполни: first_sentence, last_sentence, title (цепляющий заголовок), \
summary (одна фраза о сути), hook (что прозвучит в первые ~2 секунды — это начало \
ПЕРВОГО предложения диапазона), scores (0–5: self_contained — понятно без контекста \
ролика; hook — сила первых секунд; insight — неочевидность; emotion — эмоция/цитируемость; \
payoff — есть развязка), why (чем ценен для шортса).

Верни 10–15 лучших кандидатов.\
"""


def _user_prompt(meta: VideoMeta, sentences: list[tuple[float, float, str]]) -> str:
    peaks = heatmap_peaks(meta.heatmap)
    peak_lines = "\n".join(f"- {mmss(t)} (интенсивность {v:.2f})" for t, v in peaks)
    sent_lines = "\n".join(
        f"S{i} [{mmss(s)}] {text}" + ("" if _ENDS_CLEAN.search(text) else " ⋯")
        for i, (s, _e, text) in enumerate(sentences)
    )
    return (
        f"РОЛИК: {meta.title} (длительность {mmss(meta.duration_s)})\n\n"
        f"САМЫЕ ПЕРЕСМАТРИВАЕМЫЕ МОМЕНТЫ (heatmap):\n{peak_lines or '— нет данных —'}\n\n"
        f"ТРАНСКРИПТ ПО ПРЕДЛОЖЕНИЯМ:\n{sent_lines}"
    )


def _heatmap_boost(start_s: float, end_s: float, heatmap: list[tuple[float, float]]) -> float:
    """Максимальная интенсивность heatmap, попадающая в [start_s, end_s], * HEATMAP_SCALE."""
    vals = [v for t, v in heatmap if start_s <= t <= end_s]
    return round(max(vals, default=0.0) * HEATMAP_SCALE, 3)


def _total(scores, heatmap_boost: float) -> float:
    return round(sum(WEIGHTS[k] * getattr(scores, k) for k in WEIGHTS) + heatmap_boost, 3)


def segment(
    meta: VideoMeta,
    top_k: int = 10,
    *,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
) -> list[Segment]:
    """Вернуть top-K ранжированных кандидатов на клип."""
    cfg = Config.from_env()
    client = client or anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    model = model or cfg.analyzer_model

    sentences = build_sentences(meta.word_timings)
    if not sentences:
        raise RuntimeError("analyzer: нет пословных таймкодов — не из чего строить предложения")

    resp = client.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": _user_prompt(meta, sentences)}],
        output_format=SegmentSelection,
    )
    selection = resp.parsed_output

    scored: list[Segment] = []
    for d in selection.segments:
        i = max(0, min(d.first_sentence, len(sentences) - 1))
        j = max(i, min(d.last_sentence, len(sentences) - 1))
        # хвост оборван паузой (нет завершающей пунктуации) → дотянуть до конца фразы
        j0 = j
        while (
            j < len(sentences) - 1
            and not _ENDS_CLEAN.search(sentences[j][2])
            and j - j0 < MAX_TAIL_EXTEND_SENTENCES
            and sentences[j + 1][1] - sentences[j0][1] <= MAX_TAIL_EXTEND_S
        ):
            j += 1
        start_s, end_s = sentences[i][0], sentences[j][1]
        if not (MIN_CLIP_S <= end_s - start_s <= MAX_CLIP_S):
            log.warning(
                "drop '%s': S%s–S%s = %.0fs вне пределов %s–%ss",
                d.title, i, j, end_s - start_s, MIN_CLIP_S, MAX_CLIP_S,
            )
            continue
        transcript = " ".join(text for _s, _e, text in sentences[i : j + 1])
        boost = _heatmap_boost(start_s, end_s, meta.heatmap)
        scored.append(
            Segment(
                **d.model_dump(),
                start_s=round(start_s, 2),
                end_s=round(end_s, 2),
                transcript=transcript,
                heatmap_boost=boost,
                total=_total(d.scores, boost),
            )
        )
    scored.sort(key=lambda s: s.total, reverse=True)
    return scored[:top_k]
