"""Конфигурация goldcut — всё через переменные окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Минимальный загрузчик .env (без зависимостей). Не перезатирает заданное в окружении."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def anthropic_client(cfg: "Config"):
    """Anthropic-клиент с форсом IPv4.

    На сервере нет IPv6; резолвер иногда отдаёт AAAA-запись api.anthropic.com
    первой → httpx пытается IPv6 и падает с EAFNOSUPPORT («Connection error»).
    local_address='0.0.0.0' биндит сокет на IPv4 → только IPv4-коннекты.
    """
    import anthropic
    import httpx

    return anthropic.Anthropic(
        api_key=cfg.anthropic_api_key,
        http_client=httpx.Client(
            transport=httpx.HTTPTransport(local_address="0.0.0.0"),
            timeout=httpx.Timeout(600.0, connect=10.0),
        ),
    )


@dataclass
class Config:
    # LLM-анализ (Claude). Модель настраивается — не хардкодим в коде анализа.
    anthropic_api_key: str | None = None
    analyzer_model: str = "claude-opus-4-8"

    # Telegram
    telegram_bot_token: str | None = None

    # Fetcher: адрес воркера на Mac в tailnet
    fetcher_base_url: str = "http://macbook-air-muzaffar:8765"

    # Сколько кусков предлагать по умолчанию
    top_k: int = 10

    # Store (Postgres goldcut / goldcut_dev)
    database_url: str | None = None

    # Языки субтитров для запроса к YouTube (yt-dlp --sub-langs).
    # en+ru покрывает целевую аудиторию на бесплатных авто-сабах.
    sub_langs: str = "en-orig,en,ru-orig,ru"

    # ASR-фолбэк (пока не в Phase 1; включим для роликов без авто-сабов)
    soniox_api_key: str | None = None
    soniox_api_base: str = "https://api.soniox.com/v1"

    # Квота триала: N доставленных вырезок за скользящее окно (дней)
    trial_quota: int = 5
    trial_window_days: int = 7

    # Подписка (Telegram Stars): цена в XTR за месяц
    sub_price_xtr: int = 150

    @classmethod
    def from_env(cls, dotenv: str | Path | None = ".env") -> "Config":
        if dotenv:
            load_dotenv(dotenv)
        return cls(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            analyzer_model=os.environ.get("GOLDCUT_ANALYZER_MODEL", "claude-opus-4-8"),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            fetcher_base_url=os.environ.get(
                "GOLDCUT_FETCHER_URL", "http://macbook-air-muzaffar:8765"
            ),
            top_k=int(os.environ.get("GOLDCUT_TOP_K", "10")),
            database_url=os.environ.get("DATABASE_URL"),
            sub_langs=os.environ.get("GOLDCUT_SUB_LANGS", "en-orig,en,ru-orig,ru"),
            soniox_api_key=os.environ.get("SONIOX_API_KEY"),
            soniox_api_base=os.environ.get("SONIOX_API_BASE", "https://api.soniox.com/v1"),
            trial_quota=int(os.environ.get("GOLDCUT_TRIAL_QUOTA", "5")),
            trial_window_days=int(os.environ.get("GOLDCUT_TRIAL_WINDOW_DAYS", "7")),
            sub_price_xtr=int(os.environ.get("GOLDCUT_SUB_PRICE_XTR", "150")),
        )
