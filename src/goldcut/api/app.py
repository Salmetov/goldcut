"""goldcut API — бэкенд Telegram Mini App.

Отдаёт данные mini-app: статус аккаунта/квота, настройки (RenderProfile),
история вырезок (пруф услуги), создание Stars-инвойса подписки.

Авторизация — заголовок `X-Init-Data` (Telegram initData), проверяется HMAC по
bot-токену (api/auth.py). Никаких кук/паролей.

Запуск: uvicorn goldcut.api.app:app   (нужны DATABASE_URL, TELEGRAM_BOT_TOKEN)
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pathlib import Path

from goldcut import billing
from goldcut.api.auth import InitDataError, validate_init_data
from goldcut.config import Config
from goldcut.models import Account
from goldcut.store import Store

log = logging.getLogger("goldcut.api")

BASE = Path(__file__).resolve().parents[3]  # goldcut или goldcut-dev
CFG = Config.from_env(str(BASE / ".env"))
STORE = Store(CFG.database_url)

app = FastAPI(title="goldcut API")
# initData-авторизация не зависит от origin/кук → CORS можно широкий (в TMA всё равно один домен)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def current_account(x_init_data: str = Header(None, alias="X-Init-Data")) -> Account:
    try:
        u = validate_init_data(x_init_data or "", CFG.telegram_bot_token)
    except InitDataError as e:
        raise HTTPException(status_code=401, detail=f"auth: {e}") from e
    return STORE.get_or_create_user(u["id"], u.get("username"), u.get("language_code"))


class SettingsIn(BaseModel):
    aspect_ratio: str | None = None
    subtitles: bool | None = None
    default_mode: str | None = None


@app.get("/api/health")
def health() -> dict:
    return {"ok": STORE.ping()}


@app.get("/api/me")
def me(acc: Account = Depends(current_account)) -> dict:
    q = billing.check_quota(STORE, acc, CFG)
    return {
        "id": acc.id, "username": acc.username, "plan": acc.plan,
        "quota": {"used": q.used, "limit": q.limit, "remaining": q.remaining,
                  "window_days": q.window_days},
    }


@app.get("/api/settings")
def get_settings(acc: Account = Depends(current_account)) -> dict:
    return acc.settings.model_dump()


@app.put("/api/settings")
def put_settings(body: SettingsIn, acc: Account = Depends(current_account)) -> dict:
    if body.aspect_ratio and body.aspect_ratio not in ("original", "9:16", "1:1", "4:5", "16:9"):
        raise HTTPException(422, "bad aspect_ratio")
    if body.default_mode and body.default_mode not in ("trim", "short"):
        raise HTTPException(422, "bad default_mode")
    STORE.update_settings(
        acc.id, aspect_ratio=body.aspect_ratio, subtitles=body.subtitles,
        default_mode=body.default_mode,
    )
    return STORE.get_settings(acc.id).model_dump()


@app.get("/api/deliveries")
def deliveries(limit: int = 20, acc: Account = Depends(current_account)) -> dict:
    items = STORE.recent_deliveries(acc.id, min(max(limit, 1), 100))
    return {"items": [d.model_dump() for d in items]}


@app.post("/api/subscribe")
def subscribe(acc: Account = Depends(current_account)) -> dict:
    """Создать ссылку на Stars-инвойс месячной подписки (openInvoice в TMA)."""
    body = {
        "title": "GoldCut Premium",
        "description": "Безлимитные вырезки на месяц",
        "payload": f"sub:{acc.id}",
        "currency": "XTR",
        "prices": [{"label": "Подписка на месяц", "amount": CFG.sub_price_xtr}],
        "subscription_period": 2592000,  # 30 дней — единственный период для Stars
    }
    with httpx.Client(timeout=15) as c:
        r = c.post(
            f"https://api.telegram.org/bot{CFG.telegram_bot_token}/createInvoiceLink", json=body
        )
    data = r.json()
    if not data.get("ok"):
        log.error("createInvoiceLink failed: %s", data)
        raise HTTPException(502, f"telegram: {data.get('description')}")
    return {"invoice_url": data["result"], "price_xtr": CFG.sub_price_xtr}


# Статика mini-app (монтируется ПОСЛЕ /api-роутов — они имеют приоритет).
_WEBAPP_DIST = str(BASE / "webapp" / "dist")
if os.path.isdir(_WEBAPP_DIST):
    app.mount("/", StaticFiles(directory=_WEBAPP_DIST, html=True), name="webapp")
