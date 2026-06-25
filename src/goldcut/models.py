"""Общие модели данных goldcut (Pydantic — нужно для structured output Claude)."""

from __future__ import annotations

from pydantic import BaseModel


class Scores(BaseModel):
    """Оценки «золотистости» куска по рубрике (0–5). Заполняет LLM."""

    self_contained: float  # понятно без контекста ролика?
    hook: float            # цепляют ли первые ~3 секунды?
    insight: float         # неочевидность мысли
    emotion: float         # эмоция / цитируемость
    payoff: float          # есть ли развязка, не обрывается?


class SegmentDraft(BaseModel):
    """Кандидат на клип в том виде, как его предлагает LLM (до скоринг-математики)."""

    start_s: float          # начало, секунды от старта ролика
    end_s: float            # конец, секунды
    title: str
    summary: str            # одна фраза о сути
    hook: str               # что прозвучит в первые ~2 секунды
    transcript: str         # точный текст куска (для субтитров)
    scores: Scores
    why: str                # чем кусок ценен для шортса


class SegmentSelection(BaseModel):
    """Структурированный ответ LLM — выбранные кандидаты."""

    segments: list[SegmentDraft]


class Segment(SegmentDraft):
    """Отскоренный и ранжированный кандидат (total и heatmap_boost считает код)."""

    heatmap_boost: float = 0.0   # бонус по «самым пересматриваемым» местам YouTube
    total: float = 0.0           # итоговый взвешенный балл

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


class VideoMeta(BaseModel):
    """Лёгкие метаданные ролика — результат стадии анализа (без скачивания видео)."""

    url: str
    title: str
    duration_s: float
    transcript: str                                # с таймкодами [MM:SS]
    heatmap: list[tuple[float, float]] = []        # (sec, intensity 0..1)
    word_timings: list[tuple[float, str]] = []     # (sec, слово) — для привязки таймкодов
