"""NLU — разбор свободной реплики пользователя в структурный Request.

LLM классифицирует намерение и извлекает параметры (время, тема, желаемый
формат). Время возвращается в СЕКУНДАХ («на 12-й минуте» → 720; «1:05:30» →
3930). Работает на RU и EN. URL в реплике трогает бот отдельно — здесь только
намерение над текстом.
"""

from __future__ import annotations

import anthropic
from pydantic import BaseModel

from goldcut.config import Config, anthropic_client
from goldcut.models import RenderProfile, Request

NLU_VERSION = "v1"


class _Parsed(BaseModel):
    """Сырой структурный выход LLM (плоский — так надёжнее для парса)."""

    mode: str                       # locate | curate | other
    time_anchor_s: float | None = None
    topic: str | None = None
    length_pref_s: float | None = None
    format_mode: str | None = None       # trim | short
    format_aspect: str | None = None     # 9:16 | 1:1 | 4:5 | 16:9 | original
    format_subtitles: bool | None = None


SYSTEM = """\
Ты — парсер намерений для бота, который вырезает фрагменты из YouTube-видео. \
Пользователь пишет свободным текстом на русском или английском. Верни строгую \
структуру.

mode:
- "locate" — пользователь хочет ОДИН конкретный фрагмент по времени и/или теме \
(«дай кусок на 12 минуте про нейросети», «вырежи момент где он говорит про Маска», \
«с 5:30 по 6:10»).
- "curate" — пользователь хочет ПОДБОРКУ лучших/интересных кусков по теме \
(«вытащи самые клёвые моменты про ИИ», «сделай нарезку хайлайтов»).
- "other" — приветствие, вопрос, настройки, что-то вне двух сценариев.

time_anchor_s: время-ориентир В СЕКУНДАХ, если названо. «12-я минута»/«на 12 минуте» \
→ 720. «1:05:30» → 3930. «5:30» → 330. «в начале» → 0. «ближе к концу»/«в конце» → null \
(время неизвестно точно — тема важнее). Диапазон «с 5:30 по 6:10» → time_anchor_s=330. \
Если время не названо — null.

topic: о чём фрагмент, короткой фразой на языке пользователя. Если темы нет (только \
время) — null.

length_pref_s: желаемая длительность в секундах, если названа («секунд 30», «короткий» \
→ null, число только если явно). Иначе null.

format_mode: "trim" если просят «как есть»/«оригинал»/«просто кусок»; "short" если \
«вертикально»/«для тикток»/«9:16»/«с субтитрами как шортс». Если не сказано — null.
format_aspect: "9:16"|"1:1"|"4:5"|"16:9"|"original" если явно назвали соотношение, иначе null.
format_subtitles: true если просят субтитры/сабы, false если просят БЕЗ них, иначе null.\
"""


def parse(
    text: str,
    *,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
    cfg: Config | None = None,
) -> Request:
    cfg = cfg or Config.from_env()
    client = client or anthropic_client(cfg)
    resp = client.messages.parse(
        model=model or cfg.analyzer_model,
        max_tokens=500,
        system=SYSTEM,
        messages=[{"role": "user", "content": text}],
        output_format=_Parsed,
    )
    p = resp.parsed_output

    override: RenderProfile | None = None
    if p.format_mode or p.format_aspect or p.format_subtitles is not None:
        base = RenderProfile()
        override = RenderProfile(
            mode=p.format_mode or base.mode,
            aspect_ratio=p.format_aspect or base.aspect_ratio,
            subtitles=base.subtitles if p.format_subtitles is None else p.format_subtitles,
        )

    return Request(
        mode=p.mode if p.mode in ("locate", "curate", "other") else "other",
        time_anchor_s=p.time_anchor_s,
        topic=p.topic,
        length_pref_s=p.length_pref_s,
        format_override=override,
        raw=text,
    )
