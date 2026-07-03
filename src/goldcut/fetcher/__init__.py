"""Fetcher — добыча видео-данных. Ключевой шов системы.

Интерфейс `Fetcher` изолирует то, КАК мы достаём данные из YouTube.
Текущий бэкенд — Mac через Tailscale (см. ADR 0001), но за этим швом
его можно заменить (residential-прокси / телефон / cookies), не трогая
analyzer / cutter / bot.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from goldcut.models import VideoMeta

_YT_ID = re.compile(r"(?:youtu\.be/|[?&]v=|/shorts/|/live/)([A-Za-z0-9_-]{11})")


def youtube_id(url: str) -> str | None:
    """11-символьный ID ролика из любой формы YouTube-ссылки (ключ кэшей)."""
    m = _YT_ID.search(url)
    return m.group(1) if m else None


class Fetcher(Protocol):
    """Контракт добычи. Реализации: фетч на Mac, прокси, и т.д."""

    def meta(self, url: str) -> VideoMeta:
        """Стадия A: лёгкие данные — субтитры + heatmap + длительность.

        Видео НЕ скачивается, только текст (килобайты).
        """
        ...

    def cut(self, url: str, sections: list[tuple[float, float]]) -> list[Path]:
        """Стадия B: скачать ТОЛЬКО выбранные отрезки и вернуть файлы.

        Внутри — `yt-dlp --download-sections`, без скачивания всего ролика.
        """
        ...
