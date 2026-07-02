"""Telegram-бот goldcut: ссылка → анализ → выбор → готовые клипы.

Поток:
  1. Пользователь шлёт ссылку на YouTube.
  2. Бот: fetcher.meta (Mac, резидентный IP) → analyzer.segment (Claude) →
     список top-K кандидатов с таймкодами/хуками/баллами.
  3. Пользователь отвечает номерами («1 3 5»).
  4. Бот: fetcher.fetch_video (кэш) → cutter (9:16 + сабы) → присылает mp4.

Запуск:  python -m goldcut.bot.main   (нужен TELEGRAM_BOT_TOKEN в .env)
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from goldcut.analyzer import segment
from goldcut.config import Config
from goldcut.cutter import cut_clip
from goldcut.fetcher.client import TailscaleFetcher
from goldcut.transcript import mmss

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("goldcut.bot")

YOUTUBE_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com/(watch|shorts)|youtu\.be/)\S+")
NUMBERS_RE = re.compile(r"^[\d\s,]+$")

CFG = Config.from_env("/root/goldcut/.env")
FETCHER = TailscaleFetcher(CFG.fetcher_base_url, cache_dir="/root/goldcut/cache")
CLIPS_DIR = Path("/root/goldcut/clips")


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Кинь ссылку на YouTube-видео — найду в нём золото и предложу клипы для TikTok."
    )


def _format_candidates(segs) -> str:
    lines = ["🥇 Нашёл золото. Кандидаты на клипы:\n"]
    for i, s in enumerate(segs, 1):
        lines.append(
            f"{i}. [{mmss(s.start_s)}–{mmss(s.end_s)}] {s.title}\n"
            f"   ⚡ {s.hook}\n"
            f"   балл {s.total} · {s.duration_s:.0f}с"
        )
    lines.append("\nОтветь номерами нужных клипов через пробел (например: 1 3 5)")
    return "\n".join(lines)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = YOUTUBE_RE.search(update.message.text).group(0)
    chat = update.effective_chat
    await update.message.reply_text("🔍 Забираю транскрипт через Mac и ищу золото (~2 мин)…")
    await chat.send_action(ChatAction.TYPING)
    try:
        meta = await asyncio.to_thread(FETCHER.meta, url)
        segs = await asyncio.to_thread(segment, meta, CFG.top_k)
    except Exception as exc:
        log.exception("analyze failed")
        await update.message.reply_text(f"❌ Не получилось: {exc}")
        return
    context.user_data["pending"] = {"url": url, "meta": meta, "segs": segs}
    await update.message.reply_text(_format_candidates(segs))


async def handle_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = context.user_data.get("pending")
    if not pending:
        await update.message.reply_text("Сначала пришли ссылку на YouTube-видео.")
        return
    nums = [int(n) for n in re.findall(r"\d+", update.message.text)]
    segs = pending["segs"]
    chosen = [(n, segs[n - 1]) for n in nums if 1 <= n <= len(segs)]
    if not chosen:
        await update.message.reply_text(f"Не понял номера. Введи от 1 до {len(segs)}.")
        return

    await update.message.reply_text(
        f"✂️ Готовлю {len(chosen)} клип(а): качаю видео через Mac и режу…"
    )
    try:
        source = await asyncio.to_thread(FETCHER.fetch_video, pending["url"])
    except Exception as exc:
        log.exception("fetch_video failed")
        await update.message.reply_text(f"❌ Не смог получить видео: {exc}")
        return

    word_timings = pending["meta"].word_timings
    for n, seg_ in chosen:
        try:
            out = CLIPS_DIR / f"{source.stem}_clip{n:02d}.mp4"
            await asyncio.to_thread(cut_clip, source, seg_, out, word_timings)
            caption = f"{seg_.title}\n⚡ {seg_.hook}\n🕐 {mmss(seg_.start_s)}–{mmss(seg_.end_s)}"
            await update.effective_chat.send_action(ChatAction.UPLOAD_VIDEO)
            with open(out, "rb") as f:
                await update.message.reply_video(f, caption=caption[:1024])
        except Exception as exc:
            log.exception("clip %s failed", n)
            await update.message.reply_text(f"❌ Клип {n} не получился: {exc}")
    await update.message.reply_text("Готово. Публикуй черновиком в TikTok 🚀")


def main() -> None:
    if not CFG.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env")
    app = Application.builder().token(CFG.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Regex(YOUTUBE_RE), handle_url))
    app.add_handler(MessageHandler(filters.Regex(NUMBERS_RE) & filters.TEXT, handle_selection))
    log.info("goldcut bot: polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
