"""Store — доступ к Postgres (goldcut / goldcut_dev).

Единственный слой, знающий про SQL. Остальные модули (accounts, billing,
delivery, conversation) работают через методы Store, а не через БД напрямую.

Одно долгоживущее соединение под потокобезопасным локом (бот низконагруженный,
вызовы идут через asyncio.to_thread). Соединение переоткрывается при обрыве.
"""

from __future__ import annotations

import threading

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from goldcut.models import Account, Delivery, RenderProfile


class Store:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("Store: пустой DATABASE_URL")
        self._dsn = dsn
        self._lock = threading.Lock()
        self._conn: psycopg.Connection | None = None

    # ── соединение ────────────────────────────────────────────────
    def _c(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)
        return self._conn

    def _exec(self, sql: str, params: tuple = ()):
        with self._lock:
            try:
                return self._c().execute(sql, params)
            except psycopg.OperationalError:
                self._conn = None  # переоткрыть на следующем вызове
                return self._c().execute(sql, params)

    def ping(self) -> bool:
        return self._exec("SELECT 1").fetchone() is not None

    # ── аккаунты + настройки ──────────────────────────────────────
    def get_or_create_user(
        self, user_id: int, username: str | None = None, locale: str | None = None
    ) -> Account:
        self._exec(
            "INSERT INTO users(id, username, locale) VALUES(%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET username=COALESCE(EXCLUDED.username, users.username)",
            (user_id, username, locale),
        )
        self._exec(
            "INSERT INTO user_settings(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (user_id,)
        )
        u = self._exec("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()
        return Account(
            id=u["id"], username=u["username"], locale=u["locale"], plan=u["plan"],
            settings=self.get_settings(user_id),
        )

    def get_settings(self, user_id: int) -> RenderProfile:
        s = self._exec(
            "SELECT aspect_ratio, subtitles, default_mode FROM user_settings WHERE user_id=%s",
            (user_id,),
        ).fetchone()
        if not s:
            return RenderProfile()
        return RenderProfile(
            mode=s["default_mode"], aspect_ratio=s["aspect_ratio"], subtitles=s["subtitles"]
        )

    def update_settings(
        self,
        user_id: int,
        *,
        aspect_ratio: str | None = None,
        subtitles: bool | None = None,
        default_mode: str | None = None,
    ) -> None:
        sets, vals = [], []
        for col, val in (
            ("aspect_ratio", aspect_ratio),
            ("subtitles", subtitles),
            ("default_mode", default_mode),
        ):
            if val is not None:
                sets.append(f"{col}=%s")
                vals.append(val)
        if not sets:
            return
        sets.append("updated_at=now()")
        self._exec(f"UPDATE user_settings SET {', '.join(sets)} WHERE user_id=%s", (*vals, user_id))

    # ── квота (usage_ledger) ──────────────────────────────────────
    def usage_count(self, user_id: int, window_days: int) -> int:
        r = self._exec(
            "SELECT count(*) AS n FROM usage_ledger "
            "WHERE user_id=%s AND created_at > now() - make_interval(days => %s)",
            (user_id, window_days),
        ).fetchone()
        return int(r["n"])

    def add_usage(self, user_id: int, delivery_id: int | None) -> None:
        self._exec(
            "INSERT INTO usage_ledger(user_id, delivery_id) VALUES(%s,%s)",
            (user_id, delivery_id),
        )

    # ── доставки (история/пруф) ───────────────────────────────────
    def record_delivery(self, d: Delivery) -> int:
        r = self._exec(
            "INSERT INTO deliveries("
            "user_id, source_url, video_id, title, start_s, end_s, mode, "
            "aspect_ratio, subtitles, tg_file_id, duration_s) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                d.user_id, d.source_url, d.video_id, d.title, d.start_s, d.end_s, d.mode,
                d.aspect_ratio, d.subtitles, d.tg_file_id, d.duration_s,
            ),
        ).fetchone()
        return int(r["id"])

    def recent_deliveries(self, user_id: int, limit: int = 20) -> list[Delivery]:
        rows = self._exec(
            "SELECT * FROM deliveries WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        ).fetchall()
        return [
            Delivery(
                id=r["id"], user_id=r["user_id"], source_url=r["source_url"],
                video_id=r["video_id"], title=r["title"], start_s=r["start_s"],
                end_s=r["end_s"], mode=r["mode"], aspect_ratio=r["aspect_ratio"],
                subtitles=r["subtitles"], tg_file_id=r["tg_file_id"], duration_s=r["duration_s"],
                created_at=r["created_at"].isoformat() if r.get("created_at") else None,
            )
            for r in rows
        ]

    # ── видео-кэш индекс ──────────────────────────────────────────
    def upsert_video(
        self, video_id: str, url: str, title: str | None,
        duration_s: float | None, transcript_source: str | None,
    ) -> None:
        self._exec(
            "INSERT INTO videos(video_id, url, title, duration_s, transcript_source) "
            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT (video_id) DO UPDATE SET "
            "title=EXCLUDED.title, duration_s=EXCLUDED.duration_s, "
            "transcript_source=EXCLUDED.transcript_source, cached_at=now()",
            (video_id, url, title, duration_s, transcript_source),
        )

    # ── сессии (стейт диалога) ────────────────────────────────────
    def get_session(self, chat_id: int) -> dict:
        r = self._exec("SELECT state, current_video_id FROM sessions WHERE chat_id=%s",
                       (chat_id,)).fetchone()
        return {"state": {}, "current_video_id": None} if not r else {
            "state": r["state"] or {}, "current_video_id": r["current_video_id"]
        }

    def set_session(
        self, chat_id: int, user_id: int, state: dict, current_video_id: str | None
    ) -> None:
        self._exec(
            "INSERT INTO sessions(chat_id, user_id, state, current_video_id, updated_at) "
            "VALUES(%s,%s,%s,%s,now()) ON CONFLICT (chat_id) DO UPDATE SET "
            "user_id=EXCLUDED.user_id, state=EXCLUDED.state, "
            "current_video_id=EXCLUDED.current_video_id, updated_at=now()",
            (chat_id, user_id, Json(state), current_video_id),
        )
