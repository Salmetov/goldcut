"""Analyzer — сердце системы: отбор «золота» из транскрипта.

Реализует метод из docs/analyzer-method.md. Две задачи, намеренно НЕ слитые:
  1. Сегментация — LLM находит границы самодостаточных мыслей (по смыслу).
  2. Скоринг — код считает взвешенный балл + heatmap-буст и берёт top-K.
LLM ставит оценки по рубрике; веса и heatmap-математика живут в коде (прозрачно/тюнится).
"""

from __future__ import annotations

import anthropic

from goldcut.config import Config
from goldcut.models import Segment, SegmentSelection, VideoMeta
from goldcut.transcript import heatmap_peaks, mmss, snap_timecodes

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

# Привязанный клип принимается, только если длительность в этих пределах (сек);
# иначе выравнивание считаем неудачным и оставляем таймкоды от LLM.
SNAP_MIN_S, SNAP_MAX_S = 8.0, 120.0

SYSTEM = """\
Ты — редактор коротких вертикальных видео (TikTok/Shorts). Тебе дают транскрипт \
длинного ролика с таймкодами [MM:SS]. Твоя задача — НЕ нарезать его подряд, а \
ВЫУДИТЬ ЗОЛОТО: найти отдельные самодостаточные моменты, где спикер проговаривает \
одну ясную, неочевидную или эмоционально цепляющую мысль, которая работает как клип \
сама по себе, без контекста остального ролика.

Принципы отбора:
- Бери куски 15–60 секунд. Большая часть ролика НЕ должна дать ничего — это нормально.
- Границы — по предложениям: клип начинается с сильной фразы (хук в первые ~2 сек) и \
заканчивается развязкой, не обрываясь на полуслове.
- Каждый кусок должен быть понятен сам по себе. Если для смысла нужен контекст до/после — пропускай.
- ИГНОРИРУЙ И НЕ ВКЛЮЧАЙ: рекламные вставки и спонсорские чтения, само-промо канала \
(«подпишись», «лайк», промокоды, ссылки), организационные подводки, повторы, оффтоп, болтовню.
- Ищи разнообразие: разные мысли из разных мест ролика, а не вариации одной.

Тебе также дают список «самых пересматриваемых» моментов (heatmap) — это сигнал, где \
зрители залипают. Учитывай его, но суди самостоятельно: heatmap помогает, а не диктует.

Для каждого выбранного куска заполни:
- start_s / end_s — таймкоды в СЕКУНДАХ от начала ролика;
- title — цепляющий заголовок;
- summary — одна фраза о сути;
- hook — что прозвучит в первые ~2 секунды клипа;
- transcript — точный текст куска (для субтитров);
- scores (0–5 каждая): self_contained (понятно без контекста), hook (сила первых секунд), \
insight (неочевидность), emotion (эмоция/цитируемость), payoff (есть развязка);
- why — чем кусок ценен для шортса.

Верни 10–15 лучших кандидатов. Не добивай количество слабыми кусками — качество важнее.\
"""


def _user_prompt(meta: VideoMeta) -> str:
    peaks = heatmap_peaks(meta.heatmap)
    peak_lines = "\n".join(f"- {mmss(t)} (интенсивность {v:.2f})" for t, v in peaks)
    return (
        f"РОЛИК: {meta.title} (длительность {mmss(meta.duration_s)})\n\n"
        f"САМЫЕ ПЕРЕСМАТРИВАЕМЫЕ МОМЕНТЫ (heatmap):\n{peak_lines or '— нет данных —'}\n\n"
        f"ТРАНСКРИПТ (таймкоды [MM:SS]):\n{meta.transcript}"
    )


def _heatmap_boost(start_s: float, end_s: float, heatmap: list[tuple[float, float]]) -> float:
    """Максимальная интенсивность heatmap, попадающая в [start_s, end_s], * HEATMAP_SCALE."""
    vals = [v for t, v in heatmap if start_s <= t <= end_s]
    return round(max(vals, default=0.0) * HEATMAP_SCALE, 3)


def _total(scores, heatmap_boost: float) -> float:
    return round(sum(WEIGHTS[k] * getattr(scores, k) for k in WEIGHTS) + heatmap_boost, 3)


def _snap(draft, word_timings) -> tuple[float, float]:
    """Точные (start_s, end_s) по пословному VTT; при неудаче — таймкоды LLM."""
    if not word_timings:
        return draft.start_s, draft.end_s
    snapped = snap_timecodes(word_timings, draft.transcript)
    if snapped and SNAP_MIN_S <= (snapped[1] - snapped[0]) <= SNAP_MAX_S:
        return round(snapped[0], 2), round(snapped[1], 2)
    return draft.start_s, draft.end_s


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

    resp = client.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": _user_prompt(meta)}],
        output_format=SegmentSelection,
    )
    selection = resp.parsed_output

    scored: list[Segment] = []
    for d in selection.segments:
        start_s, end_s = _snap(d, meta.word_timings)   # точные таймкоды по пословному VTT
        boost = _heatmap_boost(start_s, end_s, meta.heatmap)
        data = d.model_dump()
        data["start_s"], data["end_s"] = start_s, end_s
        scored.append(Segment(**data, heatmap_boost=boost, total=_total(d.scores, boost)))
    scored.sort(key=lambda s: s.total, reverse=True)
    return scored[:top_k]
