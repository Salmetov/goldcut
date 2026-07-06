# goldcut — продуктовый дизайн (Phase 1)

Статус: план. Составлен 2026-07-06. Живёт с кодом, обновляется по мере реализации.

## 1. Видение

goldcut превращается из бота-нарезчика в **разговорного агента-продукт внутри Telegram** (SaaS). Две ключевые способности:

- **F1 (главная) — точечная выемка по запросу.** «Дай кусок на 12-й минуте про X» → агент находит нужный фрагмент, показывает текст+таймкод, по подтверждению режет и присылает.
- **F2 — разговорная курация.** «Вытащи лучшие куски про ИИ» → агент изучает ролик, предлагает варианты, ведёт диалог, режет только согласованное.

Обвязка продукта: аккаунты, триал/подписка (Telegram Stars), mini-app для настроек/биллинга/истории.

## 2. Принципы (наследуем)

- **Фичи — лин-MVP**, без преждевременных абстракций.
- **Архитектура/инфра — прод-уровень с самого начала**: чёткие швы, конфиг через env, компоненты разнесены.
- **Двухстадийность:** локализация/диалог — на дешёвом тексте; тяжёлый монтаж — только по подтверждению.
- **LLM выбирает, а не генерирует** (границы клипа = диапазон предложений с точным временем — не дрейфуют).
- **Подтверждение-до-нарезки** — хребет качества и точка entitlement-гейта.

## 3. Границы Phase 1

**В объёме:**
1. F1 end-to-end в чате: NLU → локализатор → подтверждение → квота-гейт → нарезка → доставка → запись в историю.
2. Фундамент SaaS: Postgres `goldcut`, аккаунты, настройки, usage-ledger, deliveries.
3. `RenderProfile` (aspect + сабы), обобщённый cutter; дефолт F1 = faithful trim.
4. Транскрипт: YouTube-сабы + **ASR-фолбэк (Soniox)** для RU/видео без сабов.
5. Entitlement-гейт (триал 5/скольз-7-дней); платежи — **заглушка-шов** (реальные Stars в P1.5).
6. Eval-скелет: golden-set + метрика IoU локализации.

**Вне Phase 1 (позже):**
- Mini-app UI (`api/` + `webapp/`) — P1.5. В P1 кладём только данные-фундамент (таблицы settings/deliveries, резолв профиля).
- Реальные платежи Stars + подписки — P1.5.
- F2 (диалоговая курация) — P2.
- Эмбеддинг-индекс длинных/кросс-видео, миграция fetcher с Mac — P3.

## 4. Карта модулей

```
store/         Postgres-доступ: users, settings, usage_ledger, deliveries, sessions, videos
accounts/      get-or-create по TG user_id; план; резолв настроек → RenderProfile
nlu/           реплика → Request(mode, time_anchor?, topic?, format_override?, length?)
retrieval/     ЛОКАЛИЗАТОР (ядро F1): locate(meta, request) → list[Candidate] + confidence
analyzer/      (есть) + topic-filter для F2; общий candidate-builder — рефактор в P2
billing/       entitlement-гейт (квота) + шов провайдера платежей (stub → Stars в P1.5)
transcript/    (есть) + ASR-фолбэк (Soniox) когда сабов нет/другой язык
cutter/        (есть) → RenderProfile (aspect 9:16|1:1|4:5|16:9|original; subs on/off; faithful trim)
delivery/      отправка клипа в TG + запись delivery-метаданных (пруф услуги)
conversation/  стейт-машина сессии (P1: линейный F1-флоу; P2: диалог F2)
jobs/          очередь тяжёлых операций (fetch_video, cut): лимит параллелизма, ретраи, cleanup
eval/          golden-set харнесс: запрос→ожидаемый спан, IoU
bot/           командный → разговорный: каждое сообщение → nlu → conversation
models.py      (есть) + Request, Candidate, RenderProfile, Delivery, Account
```

## 5. Поток данных F1

```
[TG-сообщение] → bot → accounts.get_or_create → nlu.parse → Request
   Request(url?, time_anchor?, topic?) 
      → fetcher.meta (Mac; сабы → иначе ASR-фолбэк Soniox) → VideoMeta (cache)
      → retrieval.locate(meta, request) → Candidate(range, start_s, end_s, text, confidence)
      → bot: превью «нашёл 12:03–12:41: „…“ · режем?» [Да][Раньше][Позже][Другой]
   [Да] → billing.check_and_reserve(user)      ← ENTITLEMENT-ГЕЙТ
            превышено → «5/5 за неделю, апгрейд [mini-app]»; стоп
          ок → jobs.enqueue(cut)
            → fetcher.fetch_video (кэш mp4 по tailnet)
            → cutter.render(source, candidate, RenderProfile)   ← профиль из настроек
            → delivery.send(clip) → delivery.record(...)         ← история/пруф
            → billing.commit(user, delivery_id)                  ← списание после успеха
```

Гейт на «Да» (до траты Mac/ffmpeg); списание — только после успешной доставки (упавшая нарезка не тарифицируется).

## 6. Ядро F1 — алгоритм локализации

```
locate(meta, request):
  1. anchor = request.time_anchor        # «12-я минута» → 720s (мягкий приор)
  2. window = [anchor-120s, anchor+120s] if anchor else весь_ролик
  3. sentences = build_sentences(meta.word_timings) ∩ window   # уже есть в transcript.py
  4. если request.topic:
       LLM(пронумерованные предложения окна + topic) → диапазон S_i..S_j + confidence
     иначе (только время):
       взять мысль, накрывающую anchor (ближайшее целое предложение)
  5. start,end = extend_to_sentence_bounds(...)                # целая мысль; уже есть
  6. confidence-гейт: низкий / несколько сильных → вернуть варианты, не угадывать
  7. Candidate(start_s, end_s, transcript, confidence, title)
```

Длинные ролики без time-anchor (весь транскрипт не влезает) → P3: эмбеддинг-индекс (Voyage) → окна → LLM уточняет. В P1 при отсутствии anchor и большом ролике — просим уточнить время/тему.

Версионируем метод (`LOCATOR_VERSION`), как `ANALYZER_VERSION` — кэш инвалидируется при смене промпта.

## 7. Модель данных (Postgres `goldcut`)

```sql
users(          id BIGINT PK,            -- telegram user_id
                username TEXT, locale TEXT,
                plan TEXT DEFAULT 'trial',      -- trial | paid
                created_at TIMESTAMPTZ DEFAULT now())

user_settings(  user_id BIGINT PK REFERENCES users,
                aspect_ratio TEXT DEFAULT 'original',   -- original|9:16|1:1|4:5|16:9
                subtitles BOOLEAN DEFAULT false,
                default_mode TEXT DEFAULT 'trim',       -- trim|short
                updated_at TIMESTAMPTZ DEFAULT now())

usage_ledger(   id BIGSERIAL PK, user_id BIGINT REFERENCES users,
                delivery_id BIGINT, created_at TIMESTAMPTZ DEFAULT now())
                -- квота: count(*) за скользящие 7 дней

deliveries(     id BIGSERIAL PK, user_id BIGINT REFERENCES users,
                source_url TEXT, video_id TEXT, title TEXT,
                start_s REAL, end_s REAL, mode TEXT,
                aspect_ratio TEXT, subtitles BOOLEAN,
                tg_file_id TEXT, duration_s REAL,
                created_at TIMESTAMPTZ DEFAULT now())   -- «Мои вырезки» в mini-app

sessions(       chat_id BIGINT PK, user_id BIGINT,
                state JSONB, current_video_id TEXT,
                updated_at TIMESTAMPTZ DEFAULT now())   -- заменяет PicklePersistence

videos(         video_id TEXT PK, url TEXT, title TEXT,
                duration_s REAL, transcript_source TEXT,   -- youtube_subs | soniox
                cached_at TIMESTAMPTZ DEFAULT now())

subscriptions(  user_id BIGINT PK, provider TEXT, status TEXT,
                started_at TIMESTAMPTZ, expires_at TIMESTAMPTZ,
                telegram_charge_id TEXT)                    -- заполняется в P1.5
```

Квота (триал): `SELECT count(*) FROM usage_ledger WHERE user_id=? AND created_at > now()-interval '7 days'` < 5.

## 8. RenderProfile

```
RenderProfile(aspect_ratio, subtitles: bool, mode: 'trim'|'short')
  trim  → ffmpeg -ss/-to, исходный aspect, без сабов/блюра (быстро)
  short → текущий 9:16 blur-bg + вшитые сабы
  aspect_ratio ≠ original & mode=trim → кроп/пад до соотношения без блюра
```
Резолв: `user_settings` (дефолт по mode) ← оверрайд из чат-запроса (nlu.format_override).

## 9. Транскрипт / ASR

`fetcher.meta`: сейчас `sub_langs='en.*'`. Меняем: пробуем сабы (en+ru), если нет/пусто/плохо → **ASR-фолбэк**: Mac качает аудио (yt-dlp -f bestaudio) → Soniox (у OKO уже есть ключ/паттерн) → пословные таймкоды в том же формате `word_timings`. `transcript_source` пишем в `videos`. Всё остальное (sentences/локализация/cutter) работает без изменений — единый контракт `word_timings`.

## 10. Стабильность и качество

1. **Eval-харнесс с первого дня.** Golden-set: пары «(url, запрос) → ожидаемый (start,end)». Метрика IoU таймкодов + доля «попал в мысль». `_log_segments` уже даёт данные. Регресс-гейт на промпты локализатора.
2. **Подтверждение-до-нарезки** — ловит промахи до траты ресурсов (§5).
3. **Очередь джоб** — лимит параллельных ffmpeg (CPU!), таймауты, прогресс в чат, гарант-cleanup temp, обработка «Mac офлайн».
4. **Идемпотентность** — списание квоты по факту доставки; ретрай-отправка не дублирует; кэш meta/analysis/локализации по (video_id, version).
5. **Лимиты Telegram** — Bot API 50 MB; faithful-trim длинного куска может превысить → предупреждение/сжатие/локальный Bot API (P3).
6. **Наблюдаемость** — трейс на запрос: intent → окно → кандидат → confidence → cut → delivery.
7. **Версионирование** промптов (`ANALYZER_VERSION`, `LOCATOR_VERSION`).
8. **Durable-стейт** — sessions в Postgres вместо pickle (многопользовательский продукт).

## 11. Фазировка

- **P1** — F1 в чате + фундамент (store/accounts/nlu/retrieval/cutter-profile/billing-gate/ASR/eval).
- **P1.5** — mini-app (`api/` initData-auth + `webapp/` на стеке OKO) + реальные Stars-платежи/подписка + история вырезок в UI.
- **P2** — F2 (conversation-диалог, topic-filter analyzer).
- **P3** — эмбеддинг-индекс (длинные/кросс-видео), миграция fetcher с Mac (residential-прокси/VPS), локальный Bot API.

## 12. Открытые вопросы

- Параметры подписки (цена в Stars, лимит/безлимит) — к P1.5.
- Стабильный fetcher вместо Mac — отдельный разбор перед монетизацией (P3, но решить раньше запуска платежей).
- Порог confidence локализатора — калибруется по eval-набору.
