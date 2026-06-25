"""Серверная сторона Fetcher: дёргает воркер на Mac по tailnet.

Реализует протокол `goldcut.fetcher.Fetcher`. Никакой логики yt-dlp здесь нет —
вся добыча происходит на Mac (резидентный IP). Тут только HTTP-вызовы по Tailscale.
"""

from __future__ import annotations

from pathlib import Path

from goldcut.models import VideoMeta


class TailscaleFetcher:
    """Fetcher поверх HTTP-воркера на Mac, доступного только в tailnet."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def meta(self, url: str) -> VideoMeta:  # noqa: D102
        # TODO: POST {base_url}/meta {url} -> VideoMeta
        raise NotImplementedError

    def cut(self, url: str, sections: list[tuple[float, float]]) -> list[Path]:  # noqa: D102
        # TODO: POST {base_url}/cut {url, sections} -> файлы отрезков
        raise NotImplementedError
