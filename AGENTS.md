# AGENTS.md — goldcut

Онбординг для LLM-агента (и человека), работающего над проектом. Читать первым.
Актуально на 2026-07-06 (после пивота в SaaS + F1/F2). Обновляй при изменениях.

## Что это

**goldcut** — разговорный агент в Telegram, который вырезает фрагменты из YouTube-видео
по запросу. Две способности:

- **F1 — точечная выемка:** «дай кусок на 12 минуте про X» / «вырежи с 32:53 до 34:34»
  → находит момент → превью с кнопками → по подтверждению режет и присылает.
- **F2 — разговорная курация:** «найди куски, где Кубан прожаривает OpenAI» → агент
  (Claude с инструментами) находит несколько моментов, ведёт диалог, режет согласованное.

Продукт: аккаунты по Telegram-user_id, триал (5 вырезок / скольз. 7 дней) → подписка
(Telegram Stars), mini-app для статуса/квоты/истории. По умолчанию режем **в оригинале**
(faithful trim — точная копия фрагмента); 9:16-шортс только по запросу/настройке.

Принципы: **фичи — лин-MVP**; **инфра/архитектура — прод-уровень**; **LLM выбирает, а не
генерирует** (клип = диапазон предложений с точным временем → границы не дрейфуют);
**подтверждение-до-нарезки** (дёшево на тексте, тяжёлый монтаж только по согласию).

## Быстрый старт (dev)

Всё на одном сервере (`Oko-Systems`, 90.156.223.217, Алматы). Рабочая копия — dev:

```
cd /root/goldcut-dev            # ветка dev; прод — /root/goldcut (ветка main)
source .venv/bin/activate       # или ./.venv/bin/python
# бот и API крутятся под systemd:
systemctl status goldcut-bot-dev goldcut-api-dev
journalctl -u goldcut-bot-dev -f
# после правок кода:
systemctl restart goldcut-bot-dev      # бот (polling)
systemctl restart goldcut-api-dev      # API (mini-app), если менял api/
# линт (обязательно перед коммитом):
./.venv/bin/ruff check src/
```

Dev-бот: **@goldcut_dev_bot**, mini-app **https://gcd.salmetov.fun**. Прод-бот:
**@goldcut_bot**, mini-app **https://gc.salmetov.fun** (кнопка «Аккаунт»).

## Архитектура (модули, `src/goldcut/`)

```
config.py        конфиг из .env + anthropic_client() (форс IPv4!)
models.py        pydantic: Request, Candidate, RenderProfile, Account, Delivery, VideoMeta, Segment
store/           ЕДИНСТВЕННЫЙ слой с SQL (Postgres). users/settings/usage_ledger/deliveries/
                 sessions/videos/subscriptions. Сессия диалога тоже здесь (переживает рестарт).
accounts/        get_or_create по TG user_id; resolve_profile (настройки + оверрайд из чата)
nlu/             реплика → Request (mode locate|curate|other, время, тема, формат). Claude.
retrieval/       ЛОКАЛИЗАТОР F1: время-приор → окно → LLM выбирает диапазон предложений + confidence
curator/         ЯДРО F2: find_moments (N моментов по теме) + agent.py (tool-use агент, см. ниже)
cutter/          ffmpeg по RenderProfile: trim (оригинал/кроп) | short (9:16 blur+сабы)
delivery/        отправка клипа в TG (ретраи) + запись deliveries + списание квоты
billing/         квота-гейт (check_quota) + шов PaymentProvider (StubProvider; Stars — в боте)
transcript.py    VTT → пословные таймкоды, build_sentences (канон-единицы), snap/extend
fetcher/         LocalFetcher (yt-dlp на сервере, см. «Добыча»); TailscaleFetcher — резерв за швом
bot/agent.py     Telegram-бот (polling): роутит F1/F2, платежи Stars, стейт в Postgres
api/             FastAPI mini-app: initData-auth (auth.py) + /me,/settings,/deliveries,/subscribe
                 + StaticFiles отдаёт webapp/dist (Caddy-юзер не читает /root)
eval/            golden-set + IoU для локализатора (python -m goldcut.eval)
webapp/          React+Vite mini-app (TS). Сейчас: статус/квота/Premium/история (форматы убраны)
```

**F2-агент (`curator/agent.py`) — гибрид:** Claude ведёт диалог с инструментами
`find_moments` (безопасный поиск), `cut_and_send(ids)` и `cut_range(start,end)` (режут по
согласию). «Мозг агентный, руки на рельсах»: подтверждение-до-нарезки в промпте, а квота —
жёстко в коде (`_cut_and_deliver`), LLM обойти не может. История диалога + кандидаты в сессии.

**Поток F1:** ссылка→meta → nlu → locate → превью-кнопки → квота-гейт → fetch_video → cut → deliver.
**Поток F2:** ссылка→meta → nlu=curate → agent.run_turn (find_moments → диалог → cut по «да»).

## Добыча (fetcher) — ВАЖНО

**Mac убран.** Раньше yt-dlp гонялся на Mac пользователя через Tailscale (боялись блокировки
дата-центра). Проверено: **YouTube НЕ блокирует IP этого сервера**. Теперь `LocalFetcher`
(`fetcher/local.py`) гоняет yt-dlp прямо на сервере:
- Прямой egress (proxy из окружения снят), **web-клиент + deno + yt-dlp-ejs** решают
  n-challenge → **HD 1080p** (`deno` в `/root/.deno/bin/deno`).
- Субтитры: определяем язык оригинала (`%(language)s`, обрезаем регион `en-US`→`en`) → тянем
  ОДИН авто-кэпшн этого языка (`-orig` в приоритете). Никаких списков языков.
- Видео качается ЦЕЛИКОМ один раз → кэш `cache/{video_id}.mp4` → все резы локально, без
  повторных скачиваний. Meta кэшируется в `cache/{video_id}.meta.json`.
- `TailscaleFetcher` оставлен за швом `Fetcher` — вернуть резидентную добычу, если IP заблокят.

## Инфра

- **Сервер:** `Oko-Systems` (Debian, 2 vCPU, 3.8 ГБ RAM, БЕЗ IPv6, БЕЗ GPU). Делит бокс с
  прод-продуктом OKO (`ai.salmetov.fun`) — не душить его тяжёлым (ASR/крупный рендер — нет).
- **dev/prod:** dev = `/root/goldcut-dev` (ветка `dev`, БД `goldcut_dev`, боты/юниты `*-dev`);
  prod = `/root/goldcut` (ветка `main`). Промоушен: `/root/goldcut-dev/promote.sh` (push dev
  → ff main на GitHub → prod pull → рестарт). GitHub — источник правды (HTTPS remote; push
  нужен PAT). См. [[infra_dev_prod]] в моей памяти.
- **БД:** Postgres, dev-база `goldcut_dev`, роль `goldcut_app`. Схема — `schema.sql`.
- **systemd (dev):** `goldcut-bot-dev` (bot.agent, polling), `goldcut-api-dev` (uvicorn :18090).
- **Caddy:** `gc.salmetov.fun`→`:18091` (ПРОД), `gcd.salmetov.fun`→`:18090` (DEV). API отдаёт
  и /api, и статику webapp (StaticFiles). **TLS через acme.sh DNS-01 (Spaceship)**, НЕ
  Caddy-ACME — HTTP-01 не проходит из-за алматинского inbound-флапа. Серты:
  `/etc/caddy/certs/{gc,gcd}.{crt,key}`, renewal с reloadcmd. При выпуске нужен
  `SPACESHIP_ROOT_DOMAIN=salmetov.fun`. A-записи ставятся через Spaceship API напрямую
  (PUT `spaceship.dev/api/v1/dns/records/salmetov.fun`, A использует поле `address`).
  Грабля: хук пишет «Failed to add TXT», но серт выпускается — для renewal хрупко.

## Грабли (hard-won, НЕ наступать заново)

- **IPv6:** сервера нет IPv6, но резолвер иногда отдаёт AAAA → httpx падает `[Errno 97]
  EAFNOSUPPORT` («Connection error»). Все anthropic-клиенты создавать через
  `config.anthropic_client()` (форс IPv4 через `local_address=0.0.0.0`).
- **Прокси окружения:** в интерактивном шелле стоит `HTTP_PROXY=127.0.0.1:8888` (песочница),
  ломает tailnet/прямые запросы ложным 405. Fetcher использует `trust_env=False` / снимает
  proxy-env. systemd-сервисы прокси НЕ видят (env из .env) — это только про ручные прогоны.
- **yt-dlp:** нужен `deno` + пакет `yt-dlp-ejs` (в `yt-dlp[default]`) для n-challenge/HD.
  Жадный `--sub-langs en.*,ru.*` → 429 (тянет десятки авто-переводов). Тянуть один оригинал.
- **Локаль языка:** `%(language)s` даёт `en-US`, а дорожки сабов — `en`/`en-orig`. Обрезать регион.
- **Рендер на 2 CPU:** 9:16-short (blur) 100-сек клипа ≈ 5 мин. Оригинал (trim) — быстро.
  Поэтому trim — дефолт; тяжёлый рендер прод-OKO не трогает.
- **Telegram:** Bot API — лимит 50 МБ на файл. Загрузка больших видео флапает → ретраи в delivery.
- **F2-агент:** сессия должна переживать рестарт (история в Postgres). cut_range режет
  произвольный таймкод, cut_and_send — только найденные моменты.

## Config / секреты (`.env`, chmod 600)

`DATABASE_URL`, `TELEGRAM_BOT_TOKEN` (dev-бот), `ANTHROPIC_API_KEY`, `GOLDCUT_ANALYZER_MODEL`
(claude-opus-4-8), `GOLDCUT_TRIAL_QUOTA`/`_WINDOW_DAYS`, `GOLDCUT_SUB_PRICE_XTR`. Токены в
чат/логи не светить. Прод-бот получит отдельный токен.

## Статус (2026-07-06)

Phase 1 (F1) + P1.5 (mini-app+Stars) + P2 (F2) построены и живут в dev (~27 коммитов на
ветке `dev`). Живьём в Telegram протестировано частично (F1-выемка, F2-курация, cut_range).
Дальше: докрутить по фидбеку → промоут dev→prod (прод-домен/БД/боевой токен) → возможно
склейка нескольких кусков в один ролик (ffmpeg concat), ASR-фолбэк для роликов без сабов.

Полный план и история решений: `docs/PRODUCT_DESIGN.md`, `docs/decisions/`.
