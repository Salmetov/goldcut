-- goldcut — схема БД (Postgres). Применяется идемпотентно.
-- Prod: goldcut · Dev: goldcut_dev · роль: goldcut_app

CREATE TABLE IF NOT EXISTS users (
    id          BIGINT PRIMARY KEY,             -- telegram user_id
    username    TEXT,
    locale      TEXT,
    plan        TEXT NOT NULL DEFAULT 'trial',  -- trial | paid
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id       BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    aspect_ratio  TEXT NOT NULL DEFAULT 'original',  -- original|9:16|1:1|4:5|16:9
    subtitles     BOOLEAN NOT NULL DEFAULT false,
    default_mode  TEXT NOT NULL DEFAULT 'trim',      -- trim | short
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS videos (
    video_id          TEXT PRIMARY KEY,
    url               TEXT NOT NULL,
    title             TEXT,
    duration_s        REAL,
    transcript_source TEXT,                          -- youtube_subs | soniox
    cached_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deliveries (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_url    TEXT NOT NULL,
    video_id      TEXT,
    title         TEXT,
    start_s       REAL,
    end_s         REAL,
    mode          TEXT,                              -- trim | short
    aspect_ratio  TEXT,
    subtitles     BOOLEAN,
    tg_file_id    TEXT,
    duration_s    REAL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS deliveries_user_idx ON deliveries(user_id, created_at DESC);

-- Учёт квоты: одна строка = одна затарифицированная доставка.
CREATE TABLE IF NOT EXISTS usage_ledger (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delivery_id  BIGINT REFERENCES deliveries(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_ledger_quota_idx ON usage_ledger(user_id, created_at DESC);

-- Стейт разговорной сессии (заменяет PicklePersistence).
CREATE TABLE IF NOT EXISTS sessions (
    chat_id          BIGINT PRIMARY KEY,
    user_id          BIGINT REFERENCES users(id) ON DELETE CASCADE,
    state            JSONB NOT NULL DEFAULT '{}'::jsonb,
    current_video_id TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Заполняется в P1.5 (реальные платежи Stars).
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id             BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    provider            TEXT,                        -- telegram_stars | ...
    status              TEXT,                        -- active | expired | canceled
    started_at          TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ,
    telegram_charge_id  TEXT
);
