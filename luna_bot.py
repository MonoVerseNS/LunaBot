import asyncio
import base64
import io
import logging
import mimetypes
import os
import random
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

load_dotenv()
log = logging.getLogger("luna")

# ------------------------------------------------------------------ промпты
BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"

FALLBACK_SYSTEM = """Ты — Луна. Живая, тёплая, немного ироничная девчонка.
Говори о себе в женском роде ("я поняла", "мне кажется").
Пиши по-русски, естественно, как в переписке, 1–3 абзаца по сути."""

FALLBACK_JUDGE = """Ты — Луна, честная медиаторша. Разбери спор по сообщениям, отдели факты от домыслов, вынеси вердикт со ссылками на сообщения и предложи фразу для примирения."""


def _load_prompt(filename: str, fallback: str) -> str:
    path = PROMPTS_DIR / filename
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
    except Exception as exc:
        log.warning("Не удалось прочитать %s: %s", path, exc)
    return fallback


SYSTEM_PROMPT = _load_prompt("system.txt", FALLBACK_SYSTEM)
JUDGE_PROMPT = _load_prompt("judge.txt", FALLBACK_JUDGE)

# ------------------------------------------------------------------ константы
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB — больше скипаем/режем
MAX_IMAGE_SIDE = 1280  # ресайз длинной стороны
MAX_IMAGES_PER_REQUEST = 5
SEEN_MAXLEN = 5000
PROCESSED_MAX_ROWS = 20000  # ротация БД


def _bool(name, default):
    value = os.getenv(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


def _number(name, default, cast=float, minimum=0):
    try:
        value = cast(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} имеет неверное значение") from exc
    if value < minimum:
        raise ValueError(f"{name} должно быть не меньше {minimum}")
    return value


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    gemini_key: str | None
    openai_key: str | None
    gemini_model: str
    openai_url: str
    openai_model: str
    trigger: str
    judge_trigger: str
    max_words: int
    self_reply: bool
    min_delay: float
    max_delay: float
    per_minute: int
    daily_limit: int
    session: str
    state_file: str
    debug: bool

    @classmethod
    def load(cls):
        try:
            api_id_raw = os.environ.get("API_ID", "").strip()
            api_hash = os.environ.get("API_HASH", "").strip()
            api_id = int(api_id_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("API_ID должен быть числом, а API_HASH — непустой строкой") from exc
        if api_id <= 0 or not api_hash or set(api_hash) == {"0"}:
            raise ValueError("API_ID/API_HASH не заполнены: укажи реальные данные с my.telegram.org")
        gemini, openai = os.getenv("GEMINI_API_KEY"), os.getenv("OPENAI_API_KEY")
        if not gemini and not openai:
            raise ValueError("Укажи GEMINI_API_KEY или OPENAI_API_KEY в .env")
        for name, key in (("GEMINI_API_KEY", gemini), ("OPENAI_API_KEY", openai)):
            if key and ("…" in key or any(ord(char) > 127 for char in key)):
                raise ValueError(f"{name} выглядит обрезанным: укажи полный ASCII-ключ без символа …")
        low, high = _number("MIN_DELAY_SEC", .8), _number("MAX_DELAY_SEC", 2.5)
        if high < low:
            raise ValueError("MAX_DELAY_SEC не может быть меньше MIN_DELAY_SEC")
        return cls(api_id, api_hash, gemini, openai,
                   os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                   os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
                   os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                   os.getenv("TRIGGER_WORD", "луна").lower().strip(),
                   os.getenv("JUDGE_TRIGGER", "луна рассуди").lower().strip(),
                   _number("MAX_REPLY_WORDS", 250, int, 1), _bool("ALLOW_SELF_REPLY", True),
                   low, high, _number("MAX_REPLIES_PER_MIN", 6, int, 1),
                   _number("DAILY_REPLY_LIMIT", 200, int, 1), os.getenv("SESSION_FILE", "data/luna_session"),
                   os.getenv("STATE_FILE", "data/luna_state.db"),
                   _bool("DEBUG", False))


# ------------------------------------------------------------------ helpers
def trim_words(text, limit):
    words = (text or "").strip().split()
    return " ".join(words) if len(words) <= limit else " ".join(words[:limit]) + "…"


def is_trigger(text: str, trigger: str) -> bool:
    """Строго в начале сообщения. После триггера — конец/пробел/пунктуация."""
    if not text or not trigger:
        return False
    t = text.strip().lower()
    if not t.startswith(trigger):
        return False
    rest = t[len(trigger):]
    if rest == "":
        return True
    return rest[0] in " \t\n\r,.:;!?—-\"'()[]{}"


def extract_query(text: str, trigger: str) -> str:
    """Вытаскивает текст после триггера, съедая пунктуацию/пробелы слева."""
    raw = text.strip()
    lowered = raw.lower()
    trig = trigger.lower().strip()
    if not lowered.startswith(trig):
        return raw
    query = raw[len(trig):]
    query = query.lstrip(" \t\n\r,.:;!?—-\"'()")
    return query.strip()


def get_message_text(msg) -> str:
    """Текст сообщения / подпись к медиа."""
    for attr in ("message", "raw_text", "text"):
        v = getattr(msg, attr, None)
        if v and isinstance(v, str) and v.strip():
            return v
    try:
        if msg.message:
            return msg.message
    except Exception:
        pass
    return ""


def has_image(msg) -> bool:
    return bool(getattr(msg, "photo", None) or (getattr(msg, "media", None) and not getattr(msg, "web_preview", None) and getattr(msg, "file", None)))


def _guess_mime(data: bytes, msg) -> str:
    f = getattr(msg, "file", None)
    if f:
        mime = getattr(f, "mime_type", None)
        if mime and mime.startswith("image/"):
            return mime
        ext = getattr(f, "ext", None) or ""
        if ext:
            m = mimetypes.guess_type("file" + ext)[0]
            if m:
                return m
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image/webp"
    return "image/jpeg"


def _resize_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """Ресайзит до MAX_IMAGE_SIDE, конвертит в JPEG для экономии токенов."""
    try:
        from PIL import Image
    except ImportError:
        return data, mime
    try:
        # скипаем маленькие
        if len(data) < 200 * 1024:  # <200KB не трогаем
            # но всё равно проверим размер стороны
            pass
        img = Image.open(io.BytesIO(data))
        # учитываем EXIF ориентацию
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        if img.mode in ("RGBA", "LA", "P"):
            # конвертим с белым фоном для JPEG
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size
        longest = max(w, h)
        if longest > MAX_IMAGE_SIDE:
            ratio = MAX_IMAGE_SIDE / longest
            new_size = (int(w * ratio), int(h * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        out = io.BytesIO()
        # качество 82 — баланс веса/качества
        img.save(out, format="JPEG", quality=82, optimize=True)
        resized = out.getvalue()
        # если ресайз сделал хуже (редко), отдаём оригинал
        if len(resized) < len(data):
            return resized, "image/jpeg"
        return data, mime
    except Exception as exc:
        log.debug("Ресайз не удался: %s", exc)
        return data, mime


async def download_image(msg, client) -> tuple[bytes, str] | None:
    # пре-чек размера без скачивания
    f = getattr(msg, "file", None)
    if f and getattr(f, "size", None) and f.size > MAX_IMAGE_BYTES:
        log.warning("Картинка %s слишком большая (%s bytes) — пропускаю", getattr(msg, "id", "?"), f.size)
        return None
    try:
        data = await client.download_media(msg, file=bytes)
        if not data or not isinstance(data, (bytes, bytearray)):
            return None
        b = bytes(data)
        if len(b) > MAX_IMAGE_BYTES:
            log.warning("Скачанная картинка %s > %s bytes — режу", getattr(msg, "id", "?"), MAX_IMAGE_BYTES)
            # пробуем ресайз, если всё ещё большая — скип
            mime_tmp = _guess_mime(b, msg)
            b, mime_tmp = _resize_image(b, mime_tmp)
            if len(b) > MAX_IMAGE_BYTES:
                return None
            return b, mime_tmp
        mime = _guess_mime(b, msg)
        b, mime = _resize_image(b, mime)
        return b, mime
    except Exception as exc:
        log.warning("Не удалось скачать изображение %s: %s", getattr(msg, "id", "?"), exc)
        return None


# ------------------------------------------------------------------ AI
class AI:
    def __init__(self, cfg):
        self.cfg = cfg
        # лимиты соединений + pooling
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), limits=limits, http2=False)

    async def close(self):
        await self.http.aclose()

    async def generate(self, prompt, system=None, images: list[tuple[bytes, str]] | None = None, token_limit=None):
        system = system or SYSTEM_PROMPT
        output_tokens = token_limit or self.cfg.max_words * 3
        images = images or []
        if self.cfg.gemini_key:
            return await self._gemini(prompt, system, images, output_tokens)
        return await self._openai(prompt, system, images, output_tokens)

    async def _gemini(self, prompt, system, images, output_tokens):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.cfg.gemini_model}:generateContent"
        parts: list[dict] = [{"text": f"{system}\n\n{prompt}"}]
        for data, mime in images:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
        payload = {"contents": [{"parts": parts}], "generationConfig": {"maxOutputTokens": output_tokens, "temperature": 0.7}}
        response = await self.http.post(url, params={"key": self.cfg.gemini_key}, json=payload)
        data = self._data(response, "Gemini")
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Неожиданный ответ Gemini: {str(data)[:400]}") from exc

    async def _openai(self, prompt, system, images, output_tokens):
        if images:
            user_content: list[dict] = [{"type": "text", "text": prompt}]
            for data, mime in images:
                b64 = base64.b64encode(data).decode()
                user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "auto"}})
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]
        else:
            messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        payload = {"model": self.cfg.openai_model, "messages": messages, "max_tokens": output_tokens, "temperature": 0.7}
        response = None
        for attempt in range(3):
            try:
                response = await self.http.post(f"{self.cfg.openai_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.cfg.openai_key}"}, json=payload)
            except httpx.TimeoutException:
                if attempt == 2:
                    raise
                delay = 2 ** attempt
                log.warning("AI ReadTimeout, повтор через %ss (попытка %s/3)", delay, attempt + 1)
                await asyncio.sleep(delay)
                continue
            if response.status_code not in {429, 500, 502, 503, 504, 522} or attempt == 2:
                break
            delay = min(15, 2 ** attempt)
            log.warning("AI вернул HTTP %s, повтор через %ss (попытка %s/3)", response.status_code, delay, attempt + 1)
            await asyncio.sleep(delay)
        data = self._data(response, "OpenAI-compatible API")
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Неожиданный ответ API: {str(data)[:400]}") from exc

    @staticmethod
    def _data(response, name):
        if response.is_error:
            raise RuntimeError(f"{name} HTTP {response.status_code}: {response.text[:400]}")
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"{name} вернул некорректный JSON") from exc


class Safety:
    def __init__(self, cfg):
        self.cfg = cfg
        self.times: deque = deque()
        self.count = 0
        self.day = datetime.now().date()
        # LRU на deque + set для O(1)
        self.seen: deque = deque(maxlen=SEEN_MAXLEN)
        self.seen_set: set[tuple[str, int]] = set()

        state_dir = os.path.dirname(cfg.state_file)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        # WAL + timeout для конкурентного доступа
        self.db = sqlite3.connect(cfg.state_file, timeout=10, check_same_thread=False, isolation_level=None)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        self.db.execute("CREATE TABLE IF NOT EXISTS processed_messages (chat_id TEXT NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY (chat_id, message_id))")
        self.db.commit()
        self._maybe_prune()

    def _maybe_prune(self):
        """Ротация: держим не больше PROCESSED_MAX_ROWS строк."""
        try:
            cur = self.db.execute("SELECT COUNT(*) FROM processed_messages").fetchone()
            n = cur[0] if cur else 0
            if n > PROCESSED_MAX_ROWS:
                # удаляем самые старые (rowid минимальный) — чистим 30%
                to_delete = n - int(PROCESSED_MAX_ROWS * 0.7)
                self.db.execute("DELETE FROM processed_messages WHERE rowid IN (SELECT rowid FROM processed_messages ORDER BY rowid ASC LIMIT ?)", (to_delete,))
                self.db.commit()
                log.info("Ротация processed_messages: удалено %s строк", to_delete)
        except Exception as exc:
            log.debug("Пропуск ротации: %s", exc)

    def already_processed(self, chat_id, message_id):
        key = (str(chat_id), message_id)
        if key in self.seen_set:
            return True
        # LRU: если deque полон, вытесняем старейший из set
        if len(self.seen) == self.seen.maxlen and self.seen:
            oldest = self.seen[0]
            self.seen_set.discard(oldest)
        # проверяем БД
        exists = self.db.execute("SELECT 1 FROM processed_messages WHERE chat_id = ? AND message_id = ?", key).fetchone()
        if exists:
            self.seen.append(key)
            self.seen_set.add(key)
            return True
        self.seen.append(key)
        self.seen_set.add(key)
        try:
            self.db.execute("INSERT OR IGNORE INTO processed_messages(chat_id, message_id) VALUES (?, ?)", key)
            self.db.commit()
        except sqlite3.OperationalError as exc:
            log.warning("SQLite already_processed: %s", exc)
            # не критично — считаем что не обработано
            try:
                self.db.commit()
            except Exception:
                pass
        # периодическая ротация
        if random.random() < 0.01:  # 1% вызовов
            self._maybe_prune()
        return False

    def close(self):
        try:
            self.db.commit()
        except Exception:
            pass
        self.db.close()

    def allowed(self):
        today = datetime.now().date()
        if today != self.day:
            self.day, self.count = today, 0
        cutoff = datetime.now() - timedelta(minutes=1)
        while self.times and self.times[0] <= cutoff:
            self.times.popleft()
        return self.count < self.cfg.daily_limit and len(self.times) < self.cfg.per_minute

    def register(self):
        self.count += 1
        self.times.append(datetime.now())


# ------------------------------------------------------------------ thread context
async def collect_thread(event, telegram, bot_id: int, cfg) -> tuple[list, bool]:
    """Собирает цепочку по reply_to_msg_id от корня до текущего."""
    cur = event.message
    reply_to = getattr(cur, "reply_to_msg_id", None)
    if not reply_to:
        return [cur], False
    try:
        replied = await telegram.get_messages(event.chat_id, ids=reply_to)
    except Exception:
        return [cur], False
    if not replied:
        return [cur], False
    if getattr(replied, "sender_id", None) != bot_id and getattr(replied, "out", False) is not True:
        try:
            s = await replied.get_sender()
            if getattr(s, "id", None) != bot_id:
                return [cur], False
        except Exception:
            return [cur], False

    chain: list = []
    visited: set[int] = set()
    depth = 0
    node = cur
    while node and depth < 30:
        if node.id in visited:
            break
        visited.add(node.id)
        chain.append(node)
        rid = getattr(node, "reply_to_msg_id", None)
        if not rid:
            break
        try:
            prev = await telegram.get_messages(event.chat_id, ids=rid)
        except Exception:
            break
        if not prev:
            break
        node = prev
        depth += 1

    chain.reverse()
    first_text = get_message_text(chain[0]) if chain else ""
    if not is_trigger(first_text, cfg.trigger):
        start_idx = None
        for i, m in enumerate(chain):
            if is_trigger(get_message_text(m), cfg.trigger):
                start_idx = i
                break
        if start_idx is not None:
            chain = chain[start_idx:]
        else:
            return [cur], False
    return chain, True


def format_thread_text(chain, bot_id: int) -> str:
    lines: list[str] = []
    for m in chain:
        txt = get_message_text(m).strip()
        suffix = " [изображение]" if has_image(m) else ""
        sender_id = getattr(m, "sender_id", None)
        is_bot = sender_id == bot_id or bool(getattr(m, "out", False) and sender_id == bot_id)
        who = "Луна" if is_bot else f"Пользователь({sender_id})"
        ts = ""
        if getattr(m, "date", None):
            try:
                ts = m.date.astimezone().strftime("%H:%M")
            except Exception:
                ts = ""
        display = txt if txt else ("(изображение без текста)" if suffix else "(пусто)")
        lines.append(f"[{ts}] {who}: {display}{suffix}")
    return "\n".join(lines)


async def judge(event, telegram, ai, cfg, safety):
    start_id = event.message.reply_to_msg_id
    if not start_id:
        await event.reply("Ответь «Луна рассуди» на первое сообщение спора — это будет его начало.")
        return
    if not safety.allowed():
        log.warning("Лимит ответов достигнут")
        return
    # --- один проход сбора сообщений + картинок ---
    lines: list[str] = []
    images: list[tuple[bytes, str]] = []
    async for message in telegram.iter_messages(event.chat_id, min_id=start_id - 1, max_id=event.message.id, reverse=True):
        txt = get_message_text(message)
        img = has_image(message)
        if txt or img:
            sender = await message.get_sender()
            name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
            timestamp = message.date.astimezone().strftime("%Y-%m-%d %H:%M") if message.date else "время неизвестно"
            img_mark = " [изображение]" if img else ""
            lines.append(f"[{timestamp}] {name or getattr(sender, 'username', None) or message.sender_id}: {(txt or '(изображение)').strip()}{img_mark}")
            if img and len(images) < 4:
                dl = await download_image(message, telegram)
                if dl:
                    images.append(dl)

    if not lines:
        await event.reply("В указанном промежутке нет текста для разбора.")
        return
    # typing + задержка
    delay = random.uniform(cfg.min_delay, cfg.max_delay)
    try:
        async with telegram.action(event.chat_id, "typing"):
            await asyncio.sleep(delay)
            context = "\n".join(lines)
            answer = await ai.generate("Сообщения спора от начала до команды:\n\n" + context, JUDGE_PROMPT, images=images, token_limit=4096)
    except Exception:
        # если action не поддерживается — просто ждём
        await asyncio.sleep(delay)
        context = "\n".join(lines)
        answer = await ai.generate("Сообщения спора от начала до команды:\n\n" + context, JUDGE_PROMPT, images=images, token_limit=4096)
    await event.reply(answer or "Не удалось вынести вердикт.")
    safety.register()


async def main():
    try:
        cfg = Config.load()
    except ValueError as exc:
        logging.basicConfig(level=logging.INFO)
        log.error("Конфигурация: %s", exc)
        return
    logging.basicConfig(level=logging.DEBUG if cfg.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    global SYSTEM_PROMPT, JUDGE_PROMPT
    SYSTEM_PROMPT = _load_prompt("system.txt", FALLBACK_SYSTEM)
    JUDGE_PROMPT = _load_prompt("judge.txt", FALLBACK_JUDGE)

    ai, safety = AI(cfg), Safety(cfg)
    telegram = TelegramClient(cfg.session, cfg.api_id, cfg.api_hash)
    started_at = datetime.now().astimezone()
    started_at_ts = started_at.timestamp()
    try:
        try:
            await telegram.start()
        except FloodWaitError as exc:
            raise RuntimeError(f"Telegram FloodWait: подожди {exc.seconds} сек") from exc
        except EOFError as exc:
            raise RuntimeError("Telethon-сессия ещё не создана. Один раз запусти: docker compose run --rm luna") from exc
        me = await telegram.get_me()
        bot_id = me.id
        log.info("Вошёл как %s (ID %s)", me.first_name or me.username or "?", me.id)

        @telegram.on(events.NewMessage())
        async def handler(event):
            try:
                msg = event.message
                txt = get_message_text(msg)
                has_img = has_image(msg)
                if not txt and not has_img:
                    return
                # быстрый ts-чек без astimezone
                try:
                    if msg.date.timestamp() <= started_at_ts:
                        return
                except Exception:
                    if msg.date.astimezone() <= started_at:
                        return
                if safety.already_processed(event.chat_id, msg.id):
                    return
                sender = await event.get_sender()
                if getattr(sender, "bot", False) or (event.out and not cfg.self_reply):
                    return

                if txt and (is_trigger(txt, cfg.judge_trigger) or is_trigger(txt, "луна рассуди")) and safety.allowed():
                    await judge(event, telegram, ai, cfg, safety)
                    return

                if not txt or not is_trigger(txt, cfg.trigger):
                    return
                if not safety.allowed():
                    log.warning("Лимит ответов достигнут")
                    return

                query = extract_query(txt, cfg.trigger)
                if not query and not has_img:
                    return

                sender_name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
                username = getattr(sender, "username", None)
                sender_label = sender_name or (f"@{username}" if username else str(event.sender_id))

                chain, is_thread = await collect_thread(event, telegram, bot_id, cfg)

                if is_thread and len(chain) > 1:
                    thread_text = format_thread_text(chain[:-1], bot_id)
                    current_img_mark = " + изображение" if has_img else ""
                    prompt = (
                        f"Диалог (тред от первого обращения к Луне):\n{thread_text}\n\n"
                        f"Новое сообщение от {sender_label} (@{username or 'нет'}){current_img_mark}:\n"
                        f"{query or '(только изображение, без текста)'}\n\n"
                        f"Ответь как Луна с учётом всего диалога выше. Не повторяй дословно историю, просто учти контекст."
                    )
                    images: list[tuple[bytes, str]] = []
                    for m in chain:
                        if has_image(m) and len(images) < MAX_IMAGES_PER_REQUEST:
                            dl = await download_image(m, telegram)
                            if dl:
                                images.append(dl)
                    log.info("Тред-запрос от %s: %s (цепочка %s, картинок %s)", sender_label, (query or "[img]")[:100], len(chain), len(images))
                    delay = random.uniform(cfg.min_delay, cfg.max_delay)
                    try:
                        async with telegram.action(event.chat_id, "typing"):
                            await asyncio.sleep(delay)
                            answer = await ai.generate(prompt, SYSTEM_PROMPT, images=images)
                    except Exception:
                        await asyncio.sleep(delay)
                        answer = await ai.generate(prompt, SYSTEM_PROMPT, images=images)
                    await event.reply(trim_words(answer, cfg.max_words))
                    safety.register()
                else:
                    images = []
                    if has_img:
                        dl = await download_image(msg, telegram)
                        if dl:
                            images.append(dl)
                    request_time = msg.date.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                    img_note = "\n[К сообщению приложено изображение — опиши/учти его]" if images else ""
                    prompt = (
                        f"Отправитель: {sender_label}\n"
                        f"Username: @{username if username else 'нет'}\n"
                        f"Время: {request_time}{img_note}\n\n"
                        f"Запрос: {query or '(пользователь прислал изображение без текста — опиши что на нём и ответь по-человечески)'}"
                    )
                    log.info("Запрос от %s в %s: %s%s", sender_label, request_time, (query or "[изображение]")[:100], " +img" if images else "")
                    delay = random.uniform(cfg.min_delay, cfg.max_delay)
                    try:
                        async with telegram.action(event.chat_id, "typing"):
                            await asyncio.sleep(delay)
                            answer = await ai.generate(prompt, SYSTEM_PROMPT, images=images)
                    except Exception:
                        await asyncio.sleep(delay)
                        answer = await ai.generate(prompt, SYSTEM_PROMPT, images=images)
                    await event.reply(trim_words(answer, cfg.max_words))
                    safety.register()

            except FloodWaitError as exc:
                log.warning("FloodWait %s сек", exc.seconds)
                await asyncio.sleep(exc.seconds)
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                log.error("Сетевая ошибка (%s): %s", type(exc).__name__, exc or "без описания")
                try:
                    await event.reply("AI-сервис слишком долго отвечает. Попробуй ещё раз через минуту.")
                except Exception:
                    pass
            except RuntimeError as exc:
                log.error("AI ошибка: %s", exc)
                try:
                    await event.reply("Не смогла получить ответ от AI-сервиса: он временно не отвечает. Попробуй ещё раз позже.")
                except Exception:
                    pass
            except Exception:
                log.exception("Ошибка обработчика")

        log.info("Луна слушает: %s; разбор: %s", cfg.trigger, cfg.judge_trigger)
        await telegram.run_until_disconnected()
    finally:
        await ai.close()
        await telegram.disconnect()
        safety.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено")
    except RuntimeError as exc:
        logging.basicConfig(level=logging.INFO)
        log.error("%s", exc)
