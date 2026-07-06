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
    """Кандидат от LLM: ДИАПАЗОН пронумерованных предложений, не таймкоды.

    LLM выбирает готовые единицы (S-предложения с точным временем), а не
    генерирует время/текст — поэтому границы не дрейфуют по построению.
    """

    first_sentence: int     # индекс первого предложения клипа (S-номер)
    last_sentence: int      # индекс последнего предложения (включительно)
    title: str
    summary: str            # одна фраза о сути
    hook: str               # что прозвучит в первые ~2 секунды
    scores: Scores
    why: str                # чем кусок ценен для шортса


class SegmentSelection(BaseModel):
    """Структурированный ответ LLM — выбранные кандидаты."""

    segments: list[SegmentDraft]


class Segment(SegmentDraft):
    """Готовый кандидат: код добавил точные таймкоды, дословный текст и скоринг."""

    start_s: float = 0.0         # из первого предложения диапазона
    end_s: float = 0.0           # из последнего предложения диапазона
    transcript: str = ""         # дословный текст диапазона (для сабов/проверки)
    total: float = 0.0           # итоговый взвешенный балл (только рубрика LLM)

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


# ─────────────────────────── Продукт (Phase 1) ───────────────────────────

class RenderProfile(BaseModel):
    """Как рендерить итоговый клип. Резолвится из настроек юзера + оверрайд из чата."""

    mode: str = "trim"              # trim (faithful) | short (9:16 + сабы)
    aspect_ratio: str = "original"  # original | 9:16 | 1:1 | 4:5 | 16:9
    subtitles: bool = False


class Account(BaseModel):
    """Пользователь-аккаунт (по Telegram user_id) + его настройки."""

    id: int
    username: str | None = None
    locale: str | None = None
    plan: str = "trial"             # trial | paid
    settings: RenderProfile = RenderProfile()


class Request(BaseModel):
    """Разобранная реплика пользователя (результат nlu.parse)."""

    mode: str                       # locate (F1) | curate (F2) | other
    url: str | None = None
    time_anchor_s: float | None = None   # «12-я минута» → 720.0 (мягкий приор)
    topic: str | None = None             # тема/о чём кусок
    length_pref_s: float | None = None   # желаемая длительность, если названа
    format_override: RenderProfile | None = None  # «сделай вертикально/с сабами»
    raw: str = ""                        # исходный текст (для логов/дебага)


class Candidate(BaseModel):
    """Локализованный фрагмент-кандидат (результат retrieval.locate)."""

    start_s: float
    end_s: float
    transcript: str                 # дословный текст диапазона
    title: str = ""
    confidence: float = 0.0         # 0..1 — уверенность локализатора

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


class Delivery(BaseModel):
    """Запись о доставленной вырезке (история/пруф услуги)."""

    id: int | None = None
    user_id: int
    source_url: str
    video_id: str | None = None
    title: str | None = None
    start_s: float | None = None
    end_s: float | None = None
    mode: str | None = None
    aspect_ratio: str | None = None
    subtitles: bool | None = None
    tg_file_id: str | None = None
    duration_s: float | None = None
    created_at: str | None = None
