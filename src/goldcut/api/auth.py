"""Валидация Telegram Mini App initData — «авторизация» веб-фронта.

Telegram кладёт в мини-апп подписанные данные пользователя (initData). Бэкенд
проверяет подпись HMAC-SHA256, где ключ выведен из bot-токена. Совпал хэш —
доверяем user_id; нет — 401. Паролей/сессий-кук нет: личность приходит от Telegram.

Схема (офиц. Telegram):
  secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
  check_hash = HMAC_SHA256(key=secret_key, msg=data_check_string).hex()
  data_check_string = "\\n".join(f"{k}={v}" for k,v in sorted(pairs) if k!="hash")
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InitDataError(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, *, max_age_s: int = 86400) -> dict:
    """Проверить initData и вернуть распарсенного user (dict). Бросает InitDataError."""
    if not init_data:
        raise InitDataError("empty initData")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InitDataError("no hash")

    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        raise InitDataError("bad signature")

    # свежесть (защита от реплея старого initData)
    auth_date = int(pairs.get("auth_date", "0") or 0)
    if max_age_s and auth_date and (time.time() - auth_date) > max_age_s:
        raise InitDataError("initData expired")

    user_raw = pairs.get("user")
    if not user_raw:
        raise InitDataError("no user")
    try:
        return json.loads(user_raw)
    except json.JSONDecodeError as e:
        raise InitDataError(f"bad user json: {e}") from e


def build_init_data(bot_token: str, user: dict, *, auth_date: int | None = None) -> str:
    """Собрать валидный initData (для тестов). Не для прода."""
    from urllib.parse import urlencode

    pairs = {
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "TEST",
    }
    dcs = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)
