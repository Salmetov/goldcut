"""Billing — entitlement-гейт (квота) + шов провайдера платежей.

Квота стоит в точке «подтверждение-до-нарезки»: проверяем ПЕРЕД тяжёлой джобой,
списываем ПОСЛЕ успешной доставки (упавшая нарезка не тарифицируется).

Платежи вынесены за интерфейс PaymentProvider (как fetcher-шов): в Phase 1 —
StubProvider (заглушка), в P1.5 — Telegram Stars, без правок остального кода.
"""

from __future__ import annotations

from dataclasses import dataclass

from goldcut.config import Config
from goldcut.models import Account
from goldcut.store import Store


@dataclass
class QuotaStatus:
    allowed: bool
    used: int
    limit: int | None      # None = безлимит (paid)
    window_days: int

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used)


def check_quota(store: Store, account: Account, cfg: Config | None = None) -> QuotaStatus:
    """Разрешена ли ещё одна вырезка. paid — безлимит; trial — N за окно."""
    cfg = cfg or Config.from_env()
    if account.plan == "paid":
        return QuotaStatus(allowed=True, used=0, limit=None, window_days=cfg.trial_window_days)
    used = store.usage_count(account.id, cfg.trial_window_days)
    return QuotaStatus(
        allowed=used < cfg.trial_quota,
        used=used,
        limit=cfg.trial_quota,
        window_days=cfg.trial_window_days,
    )


def commit_usage(store: Store, user_id: int, delivery_id: int | None) -> None:
    """Списать одну вырезку — вызывать только после успешной доставки."""
    store.add_usage(user_id, delivery_id)


# ─────────────────────── шов провайдера платежей ───────────────────────

class PaymentProvider:
    """Интерфейс апгрейда до платного плана. Реализация подменяется без правок кода."""

    def upgrade_prompt(self) -> str:
        raise NotImplementedError

    def create_invoice(self, user_id: int) -> str:
        raise NotImplementedError


class StubProvider(PaymentProvider):
    """Phase 1: платежей ещё нет — только текст-подсказка про апгрейд."""

    def upgrade_prompt(self) -> str:
        return "Лимит бесплатных вырезок исчерпан. Оформить подписку можно в мини-приложении (скоро)."

    def create_invoice(self, user_id: int) -> str:
        raise NotImplementedError("платежи включатся в P1.5 (Telegram Stars)")
