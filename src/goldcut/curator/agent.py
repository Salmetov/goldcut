"""F2 — разговорный агент-куратор (гибрид).

Claude ведёт диалог с двумя инструментами:
  find_moments(topic, count) — БЕЗОПАСНЫЙ поиск моментов по теме (без side-effect).
  cut_and_send(ids)          — ЖЁСТКИЙ ГЕЙТ: квота → нарезка → доставка → списание.

Мозг агентный (естественный диалог по промпту), руки на рельсах: агент обязан
спросить подтверждение до нарезки, а cut_and_send всё равно проверяет квоту в коде —
LLM не может её обойти. История диалога и найденные кандидаты живут в сессии (Postgres).
"""

from __future__ import annotations

import asyncio
import logging


from goldcut import billing, curator, delivery
from goldcut.models import Candidate, RenderProfile, VideoMeta
from goldcut.transcript import mmss

log = logging.getLogger(__name__)

MAX_TOOL_ITERS = 6
MAX_MOMENTS = 8

TOOLS = [
    {
        "name": "find_moments",
        "description": "Найти в текущем ролике до `count` моментов, отвечающих теме `topic`. "
        "Только поиск, ничего не режет. Возвращает список кандидатов с id, таймкодом, заголовком.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "тема/что ищем"},
                "count": {"type": "integer", "description": "сколько моментов (1-8)"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "cut_and_send",
        "description": "Вырезать и отправить пользователю кандидатов по их id (из последнего "
        "find_moments). Вызывай ТОЛЬКО после явного согласия пользователя. Списывает квоту.",
        "input_schema": {
            "type": "object",
            "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
            "required": ["ids"],
        },
    },
    {
        "name": "cut_range",
        "description": "Вырезать ПРОИЗВОЛЬНЫЙ таймкод-диапазон, который пользователь назвал сам "
        "(напр. «с 32:53 до 34:34»), НЕ привязываясь к найденным моментам. Так же режь непрерывный "
        "диапазон, покрывающий несколько соседних моментов (напр. 26:21–28:46 одним куском). "
        "start/end — таймкоды 'MM:SS' или 'HH:MM:SS'. Вызывай после согласия. Списывает квоту.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "начало, напр. '32:53'"},
                "end": {"type": "string", "description": "конец, напр. '34:34'"},
                "title": {"type": "string", "description": "короткий заголовок (опц.)"},
            },
            "required": ["start", "end"],
        },
    },
]

SYSTEM = """\
Ты — ассистент-монтажёр в Telegram. Пользователь прислал YouTube-видео, ты помогаешь \
ВЫРЕЗАТЬ из него нужные фрагменты через разговор.

Как работать:
- Поиск по теме — вызови find_moments(topic, count). Покажи найденное списком: номер, \
таймкод, заголовок, суть. Спроси, что резать.
- Пользователь уточняет свободным текстом («этот убери», «а есть про X», «давай 1 и 3») — \
реагируй: новая тема → снова find_moments; выбор моментов → cut_and_send(ids).
- ЕСЛИ пользователь называет КОНКРЕТНЫЙ таймкод-диапазон («вырежи с 32:53 до 34:34», \
«дай кусок 26:21–28:46 одним роликом») — режь его НАПРЯМУЮ через cut_range(start, end), \
не привязывайся к найденным моментам и не отказывай. Один непрерывный диапазон = один cut_range.
- РЕЗАТЬ только после явного согласия. Перед нарезкой коротко переспроси. Получил «да»/«давай» \
— вызывай инструмент.
- Для find_moments НЕ выдумывай таймкоды — только то, что вернул инструмент. Для cut_range — \
бери таймкоды, которые назвал пользователь. Отвечай кратко, по-русски, дружелюбно.\
"""


def _fmt_candidates(cands: list[Candidate]) -> str:
    if not cands:
        return "Ничего подходящего не нашёл."
    lines = [f"[{i}] {mmss(c.start_s)}–{mmss(c.end_s)} · {c.title} — «{c.transcript[:90]}…»"
             for i, c in enumerate(cands)]
    return "Кандидаты:\n" + "\n".join(lines)


async def _exec_find(inp: dict, ctx: "Ctx") -> str:
    topic = inp.get("topic", "")
    count = max(1, min(int(inp.get("count", 5)), MAX_MOMENTS))
    cands = await asyncio.to_thread(
        curator.find_moments, ctx.meta, topic, count, client=ctx.llm, cfg=ctx.cfg
    )
    ctx.candidates = cands
    return _fmt_candidates(cands)


def _parse_tc(s: str) -> float:
    """'MM:SS' / 'H:MM:SS' / секунды → секунды."""
    s = str(s).strip()
    if ":" in s:
        sec = 0.0
        for p in s.split(":"):
            sec = sec * 60 + float(p)
        return sec
    return float(s)


async def _cut_and_deliver(ctx: "Ctx", segments: list) -> str:
    """segments: (label, start_s, end_s, title). Гейт квоты → нарезка → доставка → списание."""
    if not billing.check_quota(ctx.store, ctx.account, ctx.cfg).allowed:
        return "GATE: лимит бесплатных вырезок исчерпан. Предложи оформить Premium."
    source = await asyncio.to_thread(ctx.fetcher.fetch_video, ctx.url)
    done, stopped = [], False
    for label, start_s, end_s, title in segments:
        if not billing.check_quota(ctx.store, ctx.account, ctx.cfg).allowed:
            stopped = True
            break
        c = Candidate(start_s=start_s, end_s=end_s, transcript="", title=title, confidence=1.0)
        out = ctx.clips_dir / f"{source.stem}_{int(start_s)}_{int(end_s)}.mp4"
        try:
            await asyncio.to_thread(delivery_render, source, c, out, ctx.profile, ctx.meta)
        except Exception as exc:
            log.exception("cut failed")
            done.append(f"{label} ошибка: {exc}")
            continue
        file_id = await ctx.send_video(out, delivery.caption_for(c, ctx.meta))
        delivery.record_and_charge(ctx.store, ctx.account.id, ctx.url, ctx.meta, c, ctx.profile, file_id)
        done.append(f"{label} ✅")
    left = billing.check_quota(ctx.store, ctx.account, ctx.cfg)
    tail = "" if left.limit is None else f" Осталось: {left.remaining}/{left.limit}."
    note = " Лимит закончился на середине — остаток не порезал." if stopped else ""
    return f"Готово: {', '.join(done)}.{tail}{note}"


async def _exec_cut(inp: dict, ctx: "Ctx") -> str:
    if not ctx.candidates:
        return "Сначала найди моменты (find_moments)."
    picked = [(i, ctx.candidates[i]) for i in inp.get("ids", []) if 0 <= i < len(ctx.candidates)]
    if not picked:
        return "Неверные id."
    return await _cut_and_deliver(ctx, [(f"#{i}", c.start_s, c.end_s, c.title) for i, c in picked])


async def _exec_cut_range(inp: dict, ctx: "Ctx") -> str:
    try:
        start_s, end_s = _parse_tc(inp["start"]), _parse_tc(inp["end"])
    except (KeyError, ValueError):
        return "Не понял таймкоды. Формат MM:SS или HH:MM:SS."
    if end_s <= start_s:
        return "Конец должен быть позже начала."
    if end_s - start_s < 3:
        return "Слишком короткий диапазон (<3с)."
    if ctx.meta.duration_s and end_s > ctx.meta.duration_s + 2:
        return f"Диапазон за пределами ролика (длительность {mmss(ctx.meta.duration_s)})."
    if end_s - start_s > 600:
        return "Слишком длинный кусок (>10 мин) — Telegram не пропустит. Возьми короче."
    label = f"{mmss(start_s)}–{mmss(end_s)}"
    return await _cut_and_deliver(ctx, [(label, start_s, end_s, inp.get("title") or label)])


def delivery_render(source, c: Candidate, out, profile: RenderProfile, meta: VideoMeta):
    from goldcut.cutter import render
    render(source, c.start_s, c.end_s, out, profile, meta.word_timings)


class Ctx:
    def __init__(self, *, url, meta, account, profile, store, fetcher, llm, cfg,
                 send_video, clips_dir, candidates):
        self.url = url
        self.meta = meta
        self.account = account
        self.profile = profile
        self.store = store
        self.fetcher = fetcher
        self.llm = llm
        self.cfg = cfg
        self.send_video = send_video
        self.clips_dir = clips_dir
        self.candidates: list[Candidate] = candidates


async def run_turn(user_text: str, messages: list, ctx: Ctx) -> tuple[str, list]:
    """Один ход диалога. Возвращает (текст ответа, обновлённые messages)."""
    messages = list(messages)
    messages.append({"role": "user", "content": user_text})
    reply = ""
    for _ in range(MAX_TOOL_ITERS):
        resp = await asyncio.to_thread(
            ctx.llm.messages.create,
            model=ctx.cfg.analyzer_model, max_tokens=1500,
            system=SYSTEM, tools=TOOLS, messages=messages,
        )
        assistant_content = []
        tool_uses = []
        for b in resp.content:
            if b.type == "text":
                assistant_content.append({"type": "text", "text": b.text})
                reply = b.text
            elif b.type == "tool_use":
                assistant_content.append(
                    {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                )
                tool_uses.append(b)
        messages.append({"role": "assistant", "content": assistant_content})
        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in tool_uses:
            if b.name == "find_moments":
                out = await _exec_find(b.input, ctx)
            elif b.name == "cut_range":
                out = await _exec_cut_range(b.input, ctx)
            else:
                out = await _exec_cut(b.input, ctx)
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})
    return reply, messages
