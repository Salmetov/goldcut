"""Delivery — отправка готового клипа в Telegram + запись факта доставки.

Отправка с ретраями (загрузка больших видео флапает). После успешной отправки
пишем запись в deliveries (пруф услуги, история для mini-app) и списываем квоту
(billing.commit_usage) — упавшая отправка не тарифицируется.
"""

from __future__ import annotations

import logging

from goldcut import billing
from goldcut.models import Candidate, Delivery, RenderProfile, VideoMeta
from goldcut.store import Store
from goldcut.transcript import mmss

log = logging.getLogger(__name__)


async def send_video(message, path, caption: str, *, retries: int = 3) -> str | None:
    """Отправить mp4 пользователю. Возвращает tg file_id или None при неудаче."""
    for attempt in range(retries):
        try:
            with open(path, "rb") as f:
                sent = await message.reply_video(f, caption=caption[:1024], supports_streaming=True)
            vid = getattr(sent, "video", None)
            return vid.file_id if vid else None
        except Exception:
            if attempt == retries - 1:
                log.exception("send_video: все %s попыток не прошли", retries)
                return None
            log.warning("send_video: попытка %s не прошла, повтор", attempt + 1)
    return None


def record_and_charge(
    store: Store,
    user_id: int,
    source_url: str,
    meta: VideoMeta,
    cand: Candidate,
    profile: RenderProfile,
    tg_file_id: str | None,
) -> Delivery:
    """Записать доставку и списать одну вырезку из квоты (после успешной отправки)."""
    from goldcut.fetcher import youtube_id

    d = Delivery(
        user_id=user_id,
        source_url=source_url,
        video_id=youtube_id(source_url),
        title=cand.title or meta.title,
        start_s=cand.start_s,
        end_s=cand.end_s,
        mode=profile.mode,
        aspect_ratio=profile.aspect_ratio,
        subtitles=profile.subtitles,
        tg_file_id=tg_file_id,
        duration_s=cand.duration_s,
    )
    d.id = store.record_delivery(d)
    billing.commit_usage(store, user_id, d.id)
    return d


def caption_for(cand: Candidate, meta: VideoMeta) -> str:
    """Подпись под клипом."""
    return (
        f"{cand.title or meta.title}\n"
        f"🕐 {mmss(cand.start_s)}–{mmss(cand.end_s)} · {cand.duration_s:.0f}с\n"
        f"🎬 {meta.title[:80]}"
    )
