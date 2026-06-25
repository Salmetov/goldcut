"""Воркер добычи — крутится НА Mac, слушает только Tailscale-интерфейс.

Оборачивает yt-dlp. Сервер (fetcher.client) обращается сюда по tailnet.
Наружу (в публичный интернет) воркер не выставляется.

Запуск (на Mac):
    uvicorn goldcut.fetcher.worker:app --host 100.119.65.77 --port 8765
"""

from __future__ import annotations

# Здесь будет лёгкое FastAPI-приложение с эндпоинтами:
#   POST /meta  {url}            -> субтитры + heatmap + длительность
#   POST /cut   {url, sections}  -> yt-dlp --download-sections, отдать файлы
#
# Реализуем после Фазы 1 (проверки ядра анализа).
