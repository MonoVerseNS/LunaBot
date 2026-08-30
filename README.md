# Luna Telegram Selfbot (Python + Telethon)

Telegram-бот по имени **Луна**, работающий через личный аккаунт (Telethon). Отвечает только когда сообщение **строго начинается** с кодового слова `луна`. Поддерживает текст, **изображения** и **контекст треда** (reply-цепочки).

Использует **Gemini ИЛИ любой OpenAI-совместимый API** для генерации ответов.

> ⚠️ **Важно:** selfbot — автоматизация личного аккаунта запрещена правилами Telegram. Использование на свой страх и риск. Меры защиты встроены, но 100% гарантии нет. Не используй в важных чатах и не спамь.

## Возможности

- **Триггер строго в начале**: `Луна привет` → ответ, `привет луна` / `лунатик` → игнор (граница слова).
- **Картинки**: подпись `Луна что на фото?` + фото → vision (Gemini `inline_data` / OpenAI `image_url`), авто-ресайз до 1280px, лимит 8MB, до 5 кадров в треде.
- **Контекст треда**: ответь на сообщение Луны с `Луна продолжи ...` — бот получит всю цепочку от первого `Луна ...` (текст + картинки).
- **Судья**: `Луна рассуди` ответом на первое сообщение спора → сбор истории + вердикт по блокам + фраза для примирения.
- **Промпты вынесены**: `prompts/system.txt`, `prompts/judge.txt` (хот-редактирование без кода).
- **Анти-бан**: `typing` индикатор, случайная задержка, лимиты, дедуп, `WAL` + ротация БД.

## Требования

- Python 3.10+ (рекомендуется 3.12)
- `API_ID` / `API_HASH` с https://my.telegram.org
- Один из ключей: `GEMINI_API_KEY` **или** `OPENAI_API_KEY`

## Быстрый старт

```bash
cd C:\Users\WhiteP\Desktop\LunaBot
cp .env.example .env   # заполни ключи
pip install -r requirements.txt
python luna_bot.py
```

При первом запуске Telethon спросит телефон и код из Telegram. Сессия сохранится в `data/luna_session.session` — повторный вход не нужен.

### Docker

```bash
cp .env.example .env   # заполни
docker compose up -d --build
docker compose logs -f luna
```

Сессия и БД в volume `luna_data` (`/app/data`). Промпты копируются в образ (`COPY prompts`).

## Настройка (.env)

| Ключ | Описание |
|------|----------|
| `API_ID` / `API_HASH` | my.telegram.org → API development tools |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | нужен один |
| `GEMINI_MODEL` | default `gemini-2.5-flash` |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | default `https://api.openai.com/v1` / `gpt-4o-mini` |
| `TRIGGER_WORD` | default `луна` |
| `JUDGE_TRIGGER` | default `луна рассуди` |
| `MAX_REPLY_WORDS` | обрезка ответа в словах, default `250` |
| `ALLOW_SELF_REPLY` | `1` — отвечать и на свои `Луна ...` (удобно в Избранном) |
| `MIN_DELAY_SEC` / `MAX_DELAY_SEC` | пауза 0.8–2.5с + `typing` |
| `MAX_REPLIES_PER_MIN` / `DAILY_REPLY_LIMIT` | default `6` / `200` |
| `SESSION_FILE` / `STATE_FILE` | default `data/luna_session` / `data/luna_state.db` |
| `DEBUG` | `1` — подробный лог |

Все числовые лимиты валидируются (`_number` с `minimum`), ключи с `…` / не-ASCII отклоняются.

## Как пользоваться

- **Обычный запрос**: `Луна сколько времени в Токио?` — ответ в 1–3 абзаца.
- **С картинкой**: прикрепи фото, в подписи `Луна опиши что на фото` — или просто `Луна` + фото без текста. Поддерживаются `photo` и `document` с `image/*`.
- **Продолжение диалога (тред)**: Луна ответила → нажми *Ответить* на её сообщение и напиши `Луна а если ...` — она получит весь тред от первого обращения.
- **Свои сообщения**: с `ALLOW_SELF_REPLY=1` можешь писать `Луна ...` в Избранном даже самому себе.
- **Разбор спора**: ответь `Луна рассуди` (строго в начале) **на первое** сообщение спора — бот соберёт всё от него до команды.

## Промпты

```
prompts/system.txt  # стиль Луны (женский род, живой русский, 1–3 абзаца)
prompts/judge.txt   # формат судьи (5 блоков)
```

Правь файлы и перезапусти — `luna_bot.py:32` подхватывает их при старте с фолбеком.

## Защита от бана (встроена)

- `typing` индикатор во время задержки/генерации
- случайная пауза `MIN_DELAY_SEC`–`MAX_DELAY_SEC`
- лимиты `MAX_REPLIES_PER_MIN` / `DAILY_REPLY_LIMIT`
- игнор ботов и защита от петель (`event.out` + `ALLOW_SELF_REPLY`)
- дедуп `processed_messages` (SQLite `WAL`, `timeout=10`, LRU `5000`, ротация `20000` строк)
- обрезка `trim_words` + `maxOutputTokens/max_tokens`

Дополнительно: отдельный аккаунт, прогрев, не спамить, не держать 24/7.

## Структура проекта

```
LunaBot/
  luna_bot.py          # основная логика (Telethon + AI)
  prompts/
    system.txt
    judge.txt
  requirements.txt     # telethon, httpx, python-dotenv, Pillow
  Dockerfile           # COPY prompts + HEALTHCHECK
  docker-compose.yml   # volumes, env, healthcheck
  .env.example         # шаблон
  .gitignore / .dockerignore
  data/                # создаётся при запуске (сессия, БД) — в .gitignore
```

## Оптимизации (что уже сделано)

- `WAL`/`LRU`/`ротация` для SQLite
- ресайз картинок до 1280px (`Pillow`, `JPEG q82`) — экономия токенов
- `judge` за 1 проход `iter_messages`
- `started_at_ts` без `astimezone()` на каждом сообщении
- `httpx.Limits(20/10)`, обработка `FloodWaitError`, `typing`

## Troubleshooting

- `API_ID/API_HASH не заполнены` → заполни `.env` с my.telegram.org
- `Укажи GEMINI_API_KEY или OPENAI_API_KEY` → нужен хотя бы один
- `…` в ключе → вставь полный ASCII-ключ
- `Telethon-сессия ещё не создана` → `docker compose run --rm luna` для первого логина
- `FloodWait: подожди N сек` → подожди, бот сам спит `N` сек
- Бот молчит → проверь `DEBUG=1`, `MAX_REPLIES_PER_MIN`/`DAILY_REPLY_LIMIT`, что сообщение **начинается** с `луна`
