"""Curator — ядро F2: поиск НЕСКОЛЬКИХ моментов по теме + разговорный агент.

find_moments: «найди куски, где X» → до N самодостаточных фрагментов, каждый по
паттерну «LLM выбирает диапазон предложений» (границы не дрейфуют, как в analyzer).
"""

from __future__ import annotations

import logging
import re

import anthropic
from pydantic import BaseModel

from goldcut.config import Config, anthropic_client
from goldcut.models import Candidate, VideoMeta
from goldcut.transcript import build_sentences, mmss

log = logging.getLogger(__name__)

CURATOR_VERSION = "v1"
MIN_CLIP_S, MAX_CLIP_S = 8.0, 180.0
_ENDS = re.compile(r"[.!?…][\"')\]]*$")


class _Moment(BaseModel):
    first_sentence: int
    last_sentence: int
    title: str
    why: str  # чем этот момент отвечает запросу


class _Moments(BaseModel):
    moments: list[_Moment]


SYSTEM = """\
Тебе дают транскрипт видео — ПРОНУМЕРОВАННЫЕ ПРЕДЛОЖЕНИЯ «S123 [MM:SS] текст» — \
и ЗАПРОС пользователя, какие моменты он хочет вырезать. Найди до N РАЗНЫХ \
самодостаточных моментов, которые точно отвечают запросу.

Каждый момент — ДИАПАЗОН предложений (first_sentence..last_sentence, включительно), \
который читается как цельное законченное высказывание по теме запроса. Правила:
- Включай контекст, если ключевая фраза опирается на предыдущие (this/that/ответ на вопрос).
- Начинай с сильного предложения, а не с вялой подводки.
- 15–90 секунд на момент (ориентируйся по [MM:SS]).
- Только РЕАЛЬНЫЕ совпадения с запросом. Если хороших моментов меньше N — верни меньше. \
Если совсем нет — верни пустой список. Качество важнее количества.
- Моменты РАЗНЫЕ и из разных мест ролика, не пересекаются.
- НЕ включай рекламу, само-промо, оргболтовню.

Для каждого: first_sentence, last_sentence, title (короткий заголовок), why (чем отвечает запросу).\
"""


def find_moments(
    meta: VideoMeta,
    topic: str,
    count: int = 5,
    *,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
    cfg: Config | None = None,
) -> list[Candidate]:
    """До `count` фрагментов, отвечающих теме. Пустой список — не нашлось."""
    cfg = cfg or Config.from_env()
    sentences = build_sentences(meta.word_timings)
    if not sentences:
        raise RuntimeError("find_moments: нет пословных таймкодов")

    client = client or anthropic_client(cfg)
    sent_lines = "\n".join(f"S{i} [{mmss(s)}] {t}" for i, (s, _e, t) in enumerate(sentences))
    user = (
        f"РОЛИК: {meta.title}\nЗАПРОС: {topic}\nN = {count}\n\n"
        f"ПРЕДЛОЖЕНИЯ:\n{sent_lines}"
    )
    resp = client.messages.parse(
        model=model or cfg.analyzer_model,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=_Moments,
    )

    out: list[Candidate] = []
    for m in resp.parsed_output.moments:
        i = max(0, min(m.first_sentence, len(sentences) - 1))
        j = max(i, min(m.last_sentence, len(sentences) - 1))
        # дотянуть хвост до конца фразы, если оборван паузой
        while j < len(sentences) - 1 and not _ENDS.search(sentences[j][2]) and j - i < 3:
            j += 1
        start_s, end_s = sentences[i][0], sentences[j][1]
        dur = end_s - start_s
        if not (MIN_CLIP_S <= dur <= MAX_CLIP_S):
            log.warning("find_moments drop '%s': %.0fs вне пределов", m.title, dur)
            continue
        text = " ".join(t for _s, _e, t in sentences[i : j + 1])
        out.append(Candidate(start_s=round(start_s, 2), end_s=round(end_s, 2),
                             transcript=text, title=m.title, confidence=1.0))
    return out[:count]
