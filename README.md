# Luna Telegram Selfbot (Python + Telethon)

Telegram-бот по имени **Луна**, работающий через личный аккаунт (Telethon). Отвечает только когда сообщение **строго начинается** с кодового слова `луна`. Поддерживает текст, **изображения** и **контекст треда** (reply-цепочки).

Использует **Gemini ИЛИ любой OpenAI-совместимый API** для генерации ответов.

> ⚠️ **Важно:** selfbot — автоматизация личного аккаунта запрещена правилами Telegram. Использование на свой страх и риск. Меры защиты встроены, но 100% гарантии нет. Не используй в важных чатах и не спамь.

## Возможности

- **Триггер строго в начале**: `Луна привет` → ответ, `привет луна` / `лунатик` → игнор (граница слова).
- **Картинки**: подпись `Луна что на фото?` + фото → vision (Gemini `inline_data` / OpenAI `image_url`), авто-ресайз до 1280px, лимит 8MB, до 5 кадров в треде.
- **Контекст треда**: ответь на сообщение Луны с `Луна продолжи ...` — бот получит всю цепочку от первого `Луна ...` (текст + картинки).
- **Цитата чужого сообщения**: `Луна что думаешь?` ответом на любое сообщение (не обязательно от Луны) — цитата подтянется в контекст автоматически, даже если её нет в истории треда.
- **Веб-поиск**: к каждому запросу автоматически подтягиваются свежие данные из интернета (бесплатный DuckDuckGo, опционально Tavily через `TAVILY_API_KEY`) — Луна отвечает по актуальной информации, а не только по знаниям модели.
- **Судья**: `Луна рассуди` ответом на первое сообщение спора → сбор истории + жёсткий вердикт с занятием стороны (без сглаживания углов, с опорой на знания ИИ и свежие данные).
- **Фильтры чатов**: `CHAT_WHITELIST` / `CHAT_BLACKLIST` — взаимоисключающие, заполняется только один (ID или username через запятую); `IGNORE_CHANNELS=1` — игнор broadcast-каналов, `IGNORE_GROUPS=1` — игнор групп (включая комментарии).
- **Промпты вынесены**: `prompts/system.txt`, `prompts/judge.txt` (хот-редактирование без кода).
- **Анти-бан**: `typing` индикатор, случайная задержка, лимиты, дедуп, `WAL` + ротация БД.

## Требования

- Python 3.10+ (рекомендуется 3.12)
- `telegram.api_id` / `api_hash` с https://my.telegram.org
- Один из ключей: `ai.gemini_key` **или** `ai.openai_key`

## Быстрый старт

```bash
cd C:\Users\WhiteP\Desktop\LunaBot
cp config.example.json config.json   # заполни ключи
pip install -r requirements.txt
python luna_bot.py
```

Есть старый `.env`? Конвертни: `python tools/migrate_env_to_json.py .env config.json`.

При первом запуске Telethon спросит телефон и код из Telegram. Сессия сохранится в `data/luna_session.session` — повторный вход не нужен.

### Docker (один бот)

```bash
cp config.example.json config.json   # заполни
docker compose up -d --build
docker compose logs -f luna
```

Сессия и БД в volume `luna_data` (`/app/data`). Промпты копируются в образ (`COPY prompts`).

### Флот (2–3 бота на VPS, см. `TZ_FLEET.md`)

```bash
LUNA_IMAGE=registry/luna:v1.0 docker compose -f docker-compose.fleet.yml up -d
```

Положи `config.json` каждого клиента в `clients/a/`, `clients/b/` (генерирует Fleet Bot).
Образ один на всех — только `pull`, сборки на месте нет. У каждого сервиса свой volume,
лимит RAM 512 МБ и ротация логов.

## Настройка (`config.json`)

Единственная env-переменная — `CONFIG_FILE` (путь к конфигу, default `./config.json`).
Всё остальное — в JSON. Имена команд (`рассуди`, `перескажи` и будущие) живут в коде (`COMMANDS`
в `luna_bot.py`) и приклеиваются к триггеру автоматически — переименование бота их не ломает.

| Поле | Описание |
|------|----------|
| `bot_name` / `trigger` | имя и слово-триггер, default `Луна` / `луна` |
| `character` | пресет из `prompts/characters/*.txt`, default `luna-classic` |
| `telegram.api_id` / `api_hash` | my.telegram.org → API development tools |
| `ai.gemini_key` / `ai.openai_key` | нужен один |
| `ai.gemini_model` | default `gemini-2.5-flash` |
| `ai.openai_base_url` / `ai.openai_model` | default `https://api.openai.com/v1` / `gpt-4o-mini` |
| `filters.whitelist` / `blacklist` | списки чатов (ID/username); взаимоисключающие |
| `filters.user_whitelist` | только эти пользователи (ID/username); пусто = все |
| `filters.ignore_channels` | игнор broadcast-каналов (default `true`) |
| `filters.ignore_groups` | игнор групп и комментариев (default `false`) |
| `search.enabled` / `max_results` | свежие данные к каждому запросу, default `true` / `5` |
| `search.tavily_key` | опционально; без него DuckDuckGo |
| `limits.*` | `max_reply_words` (250), `allow_self_reply`, `min/max_delay_sec`, `per_minute` (6), `daily` (200) |
| `device.*` | `model` / `system` / `app_version` / `lang` — уникальный фингерпринт устройства на бота |
| `paths.session` / `state` | default `data/luna_session` / `data/luna_state.db` |
| `debug` | подробный лог |

Все значения валидируются при старте, ключи с `…` / не-ASCII отклоняются.

## Как пользоваться

- **Обычный запрос**: `Луна сколько времени в Токио?` — ответ в 1–3 абзаца.
- **С картинкой**: прикрепи фото, в подписи `Луна опиши что на фото` — или просто `Луна` + фото без текста. Поддерживаются `photo` и `document` с `image/*`.
- **Продолжение диалога (тред)**: Луна ответила → нажми *Ответить* на её сообщение и напиши `Луна а если ...` — она получит весь тред от первого обращения.
- **Свои сообщения**: с `ALLOW_SELF_REPLY=1` можешь писать `Луна ...` в Избранном даже самому себе.
- **Разбор спора**: ответь `Луна рассуди` (строго в начале) **на первое** сообщение спора — бот соберёт всё от него до команды и вынесет вердикт с занятием стороны.
- **Выжимка чата**: `Луна перескажи` — краткое саммари последних 50 сообщений; `Луна перескажи 30` — нужное число (максимум 100). Ответом на сообщение — выжимка от него до команды.
- **Экспорт диалога**: `Луна экспорт` — выгрузка последних 50 сообщений в текстовый файл; `Луна экспорт 100` — своё число (максимум 200). Ответом на сообщение — выгрузка от него до команды.
- **Перевод**: `Луна переведи на английский` — перевод последнего чужого сообщения (или того, на которое ответили).
- **Вопрос про чужое сообщение**: ответь `Луна ...` на любое сообщение — его текст подтянется как цитата.
- **Свежие факты**: к каждому ответу бот сам подтягивает актуальные данные из интернета.

## Промпты и характеры

```
prompts/system.txt                # базовый стиль (фолбек)
prompts/judge.txt                 # формат судьи (5 блоков, вердикт со стороной)
prompts/characters/
  luna-classic.txt                # тёплая, ироничная (default)
  luna-derzkaya.txt               # дерзкая, острая на язык
  luna-strogaya.txt               # строгая, по делу
```

Поле `character` в `config.json` выбирает пресет. Правь файлы и перезапусти —
подхватываются при старте с фолбеком.

## Защита от бана (встроена)

- `typing` индикатор во время задержки/генерации
- случайная пауза `min/max_delay_sec` (генерируй разные каждому боту!)
- лимиты `per_minute` / `daily` (глобальные) + `per_chat_per_minute` / `per_chat_daily` (per-chat)
- игнор ботов и защита от петель (`event.out` + `allow_self_reply`)
- дедуп `processed_messages` (SQLite `WAL`, `timeout=10`, LRU `5000`, ротация `20000` строк)
- дедуп по рестарту: сообщения до `last_restart` пропускаются
- обрезка `trim_words` + `maxOutputTokens/max_tokens`
- уникальный device-фингерпринт на бота (`device.*` в конфиге)
- circuit breaker для AI: после 3 ошибок подряд — отказ на 60 сек
- кэш веб-поиска на 10 минут (не дублирует запросы)
- чистка entity cache Telethon раз в час (борьба с ростом RAM)
- горячая перезагрузка конфига по SIGHUP (без рестарта)
- обработка SIGTERM для корректного завершения

Дополнительно: отдельный аккаунт, прогрев, не спамить, у каждого VPS свой IP.

## Структура проекта

```
LunaBot/
  luna_bot.py                # основная логика (Telethon + AI)
  config.example.json        # шаблон конфига (config.json — в .gitignore)
  prompts/
    system.txt               # фолбек стиля
    judge.txt                # судья
    characters/              # пресеты характеров на выбор
  tools/
    migrate_env_to_json.py   # разовая миграция старого .env
  requirements.txt           # telethon, httpx, Pillow
  Dockerfile                 # COPY prompts + HEALTHCHECK
  docker-compose.yml         # один бот локально
  docker-compose.fleet.yml   # 2–3 бота на VPS (образ по тегу, лимиты, ротация логов)
  TZ_FLEET.md                # ТЗ системы продаж и автоматизации
  clients/                   # config.json клиентов (генерирует Fleet Bot) — в .gitignore
  data/                      # создаётся при запуске (сессия, БД) — в .gitignore
```

## Оптимизации (что уже сделано)

- `WAL`/`LRU`/`ротация` для SQLite
- ресайз картинок до 1280px (`Pillow`, `JPEG q82`) — экономия токенов
- `judge` за 1 проход `iter_messages`
- `started_at_ts` без `astimezone()` на каждом сообщении
- `httpx.Limits(20/10)`, обработка `FloodWaitError`, `typing`
- единая точка ответа `answer_with_typing` (без трипликации кода)

## Troubleshooting

- `Конфиг не найден` → скопируй `config.example.json` в `config.json`
- `telegram.api_id/api_hash не заполнены` → заполни с my.telegram.org
- `Нужен ai.gemini_key или ai.openai_key` → нужен хотя бы один
- `whitelist и blacklist взаимоисключающие` → оставь только один список
- `…` в ключе → вставь полный ASCII-ключ
- `Telethon-сессия ещё не создана` → `docker compose run --rm luna` для первого логина
- `FloodWait: подожди N сек` → подожди, бот сам спит `N` сек
- Бот молчит → проверь `debug`, `limits`, что сообщение **начинается** с триггера
