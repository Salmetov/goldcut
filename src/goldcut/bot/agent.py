"""goldcut — разговорный агент (Phase 1, Feature 1: точечная выемка по запросу).

Флоу:
  1. ссылка на YouTube → fetcher.meta → «ролик загружен, что вырезать?»
  2. свободный запрос («дай кусок на 12 минуте про X») → nlu → retrieval.locate
     → превью (текст+таймкод) + кнопки [✂️ Режь] [⬅️ Раньше] [➡️ Позже] [✖️]
  3. [Режь] → квота-гейт → fetch_video → cutter.render(профиль) → доставка + запись
Стейт сессии — в Postgres (store.sessions), переживает рестарт бота.

Запуск: python -m goldcut.bot.agent   (нужен TELEGRAM_BOT_TOKEN, DATABASE_URL в .env)
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import anthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from goldcut import accounts, billing, delivery, nlu
from goldcut.config import Config
from goldcut.cutter import render
from goldcut.fetcher.local import LocalFetcher
from goldcut.models import Candidate
from goldcut.retrieval import locate
from goldcut.store import Store
from goldcut.transcript import mmss

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("goldcut.agent")

CFG = Config.from_env("/root/goldcut-dev/.env")
STORE = Store(CFG.database_url)
FETCHER = LocalFetcher(
    cache_dir="/root/goldcut-dev/cache", ytdlp="/root/goldcut-dev/.venv/bin/yt-dlp"
)
LLM = anthropic.Anthropic(api_key=CFG.anthropic_api_key)
CLIPS = Path("/root/goldcut-dev/clips")
YOUTUBE_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com/(watch|shorts)|youtu\.be/)\S+")
NUDGE_S = 15.0
TG_SIZE_LIMIT = 49 * 1024 * 1024

BILLING_PROVIDER = billing.StubProvider()


# ────────────────────────── сессия ──────────────────────────
def _load(chat_id: int) -> dict:
    return STORE.get_session(chat_id)["state"] or {}


def _save(chat_id: int, user_id: int, state: dict) -> None:
    STORE.set_session(chat_id, user_id, state, state.get("video_id"))


# ────────────────────────── команды ──────────────────────────
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    STORE.get_or_create_user(u.id, u.username, u.language_code)
    await update.message.reply_text(
        "Привет! Пришли ссылку на YouTube-видео, потом напиши, какой кусок нужен — "
        "например «дай фрагмент на 12 минуте про нейросети». Я найду момент, покажу текст, "
        "и после подтверждения вырежу и пришлю.\n\nБесплатно: 5 вырезок в неделю."
    )


# ────────────────────────── ссылка ──────────────────────────
async def on_url(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    url = YOUTUBE_RE.search(update.message.text).group(0)
    chat, u = update.effective_chat, update.effective_user
    STORE.get_or_create_user(u.id, u.username, u.language_code)
    await update.message.reply_text("🔍 Забираю транскрипт…")
    await chat.send_action(ChatAction.TYPING)
    try:
        meta = await asyncio.to_thread(FETCHER.meta, url)
    except Exception as exc:
        log.exception("meta failed")
        if "субтитры" in str(exc).lower():
            await update.message.reply_text(
                "У этого ролика нет субтитров (en/ru) — пока поддерживаю только видео с "
                "автосубтитрами. Пришли другую ссылку."
            )
        else:
            await update.message.reply_text(f"❌ Не смог получить видео: {exc}")
        return
    from goldcut.fetcher import youtube_id
    vid = youtube_id(url)
    STORE.upsert_video(vid, url, meta.title, meta.duration_s, "youtube_subs")
    _save(chat.id, u.id, {"url": url, "video_id": vid})
    await update.message.reply_text(
        f"📄 Готово: «{meta.title[:70]}» ({mmss(meta.duration_s)}).\n"
        "Что вырезать? Напиши время и/или тему — например «на 8 минуте про X» или «момент про Y»."
    )


# ────────────────────────── запрос (текст) ──────────────────────────
def _preview(cand: Candidate) -> tuple[str, InlineKeyboardMarkup]:
    conf = "🟢" if cand.confidence >= 0.75 else "🟡" if cand.confidence >= 0.5 else "🟠"
    text = (
        f"{conf} Нашёл: {mmss(cand.start_s)}–{mmss(cand.end_s)} ({cand.duration_s:.0f}с)\n\n"
        f"«{cand.transcript[:350]}{'…' if len(cand.transcript) > 350 else ''}»\n\nРежем?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✂️ Режь", callback_data="cut")],
        [InlineKeyboardButton("⬅️ Раньше", callback_data="earlier"),
         InlineKeyboardButton("➡️ Позже", callback_data="later")],
        [InlineKeyboardButton("✖️ Отмена", callback_data="cancel")],
    ])
    return text, kb


async def on_text(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat, u = update.effective_chat, update.effective_user
    st = _load(chat.id)
    if not st.get("url"):
        await update.message.reply_text("Сначала пришли ссылку на YouTube-видео.")
        return
    await chat.send_action(ChatAction.TYPING)
    req = await asyncio.to_thread(nlu.parse, update.message.text, client=LLM, cfg=CFG)

    if req.mode == "curate":
        await update.message.reply_text("Подборку лучших кусков (F2) добавлю позже. Пока умею точечно — назови момент.")
        return
    if req.mode == "other":
        await update.message.reply_text("Назови момент: время и/или тему. Напр. «на 15 минуте про безопасность».")
        return

    try:
        meta = await asyncio.to_thread(FETCHER.meta, st["url"])
        cands = await asyncio.to_thread(locate, meta, req, client=LLM, cfg=CFG)
    except Exception as exc:
        log.exception("locate failed")
        await update.message.reply_text(f"❌ Ошибка поиска: {exc}")
        return
    if not cands:
        await update.message.reply_text("Не нашёл такой момент. Уточни время или тему.")
        return

    acc = STORE.get_or_create_user(u.id, u.username, u.language_code)
    profile = accounts.resolve_profile(acc, req)
    c = cands[0]
    st["pending"] = {"start_s": c.start_s, "end_s": c.end_s, "transcript": c.transcript,
                     "title": c.title, "confidence": c.confidence,
                     "profile": profile.model_dump()}
    _save(chat.id, u.id, st)
    text, kb = _preview(c)
    await update.message.reply_text(text, reply_markup=kb)


# ────────────────────────── кнопки ──────────────────────────
async def on_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    chat_id, u = q.message.chat_id, update.effective_user
    st = _load(chat_id)
    p = st.get("pending")
    if not p:
        await q.edit_message_text("Сессия истекла. Пришли ссылку и запрос заново.")
        return

    if q.data == "cancel":
        st.pop("pending", None)
        _save(chat_id, u.id, st)
        await q.edit_message_text("Отменил.")
        return

    if q.data in ("earlier", "later"):
        shift = -NUDGE_S if q.data == "earlier" else NUDGE_S
        p["start_s"] = max(0.0, p["start_s"] + shift)
        p["end_s"] = p["end_s"] + shift
        _save(chat_id, u.id, st)
        c = Candidate(**{k: p[k] for k in ("start_s", "end_s", "transcript", "title", "confidence")})
        text, kb = _preview(c)
        await q.edit_message_text(text + f"\n\n(сдвинул на {int(shift):+d}с)", reply_markup=kb)
        return

    if q.data == "cut":
        acc = STORE.get_or_create_user(u.id, u.username, u.language_code)
        quota = billing.check_quota(STORE, acc, CFG)
        if not quota.allowed:
            await q.edit_message_text(f"⛔ {BILLING_PROVIDER.upgrade_prompt()}")
            return
        await q.edit_message_text("✂️ Режу: качаю видео и монтирую (может занять минуту)…")
        try:
            meta = await asyncio.to_thread(FETCHER.meta, st["url"])
            source = await asyncio.to_thread(FETCHER.fetch_video, st["url"])
            from goldcut.models import RenderProfile
            profile = RenderProfile(**p["profile"])
            c = Candidate(**{k: p[k] for k in ("start_s", "end_s", "transcript", "title", "confidence")})
            out = CLIPS / f"{Path(source).stem}_{int(c.start_s)}_{int(c.end_s)}.mp4"
            await asyncio.to_thread(render, source, c.start_s, c.end_s, out, profile, meta.word_timings)
        except Exception as exc:
            log.exception("cut failed")
            await q.edit_message_text(f"❌ Не получилось: {exc}")
            return
        if out.stat().st_size > TG_SIZE_LIMIT:
            await q.message.reply_text("⚠️ Клип больше 50 МБ — Telegram не пропустит. Возьми кусок покороче.")
            return
        await q.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
        file_id = await delivery.send_video(q.message, out, delivery.caption_for(c, meta))
        if not file_id and not out.exists():
            await q.message.reply_text("❌ Не смог отправить клип.")
            return
        delivery.record_and_charge(STORE, u.id, st["url"], meta, c, profile, file_id)
        left = billing.check_quota(STORE, acc, CFG)
        tail = "" if left.limit is None else f"\nОсталось бесплатных: {left.remaining}/{left.limit}"
        st.pop("pending", None)
        _save(chat_id, u.id, st)
        await q.message.reply_text("Готово ✅" + tail)


def main() -> None:
    if not CFG.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан")
    if not STORE.ping():
        raise SystemExit("нет связи с БД (DATABASE_URL)")
    CLIPS.mkdir(parents=True, exist_ok=True)
    app = (
        Application.builder().token(CFG.telegram_bot_token)
        .connect_timeout(10).read_timeout(60).write_timeout(60)
        .media_write_timeout(600).pool_timeout(10).build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Regex(YOUTUBE_RE), on_url))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("goldcut agent: polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
