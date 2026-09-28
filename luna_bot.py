import asyncio
import base64
import html
import io
import json
import logging
import mimetypes
import os
import random
import re
import signal
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

log = logging.getLogger("luna")

# ------------------------------------------------------------------ промпты
BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"

FALLBACK_SYSTEM = """Ты — Луна. Живая, тёплая, немного ироничная девчонка.
Говори о себе в женском роде ("я поняла", "мне кажется").
Пиши по-русски, естественно, как в переписке, 1–3 абзаца по сути."""

FALLBACK_JUDGE = """Ты — Луна, прямой и честный арбитр. Разбери спор, займи сторону по фактам и логике, без сглаживания углов. Если данных мало — выскажи свою позицию на основе знаний. Ссылайся на конкретные сообщения."""


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


def _load_character(name: str) -> str:
    """Пресет характера из prompts/characters/<name>.txt, фолбек — system.txt."""
    safe = re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9_-]", "", (name or "").strip())
    if safe:
        text = _load_prompt(f"characters/{safe}.txt", "")
        if text:
            return text
    return _load_prompt("system.txt", FALLBACK_SYSTEM)


SYSTEM_PROMPT = _load_prompt("system.txt", FALLBACK_SYSTEM)
JUDGE_PROMPT = _load_prompt("judge.txt", FALLBACK_JUDGE)

# ------------------------------------------------------------------ константы
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB — больше скипаем/режем
MAX_IMAGE_SIDE = 1280  # ресайз длинной стороны
MAX_IMAGES_PER_REQUEST = 5
SEEN_MAXLEN = 5000
PROCESSED_MAX_ROWS = 20000  # ротация БД
ENTITY_CACHE_CLEAR_INTERVAL = 3600  # чистка entity cache Telethon раз в час
SEARCH_CACHE_TTL = 600  # кэш веб-поиска 10 минут
CIRCUIT_BREAKER_THRESHOLD = 3  # подряд ошибок AI до открытия цепи
CIRCUIT_BREAKER_TIMEOUT = 60  # секунд до повторной попытки после сбоя
MAX_HISTORY_CHARS = 3000  # обрезка длинных сообщений в истории для ИИ

# Имена команд — в коде, не в конфиге. Команда = "<триггер> <слово>",
# переименование бота команды не ломает: «луна рассуди», «злата рассуди», ...
COMMANDS = {
    "judge": "рассуди",    # разбор спора ответом на первое сообщение
    "summary": "перескажи",  # выжимка последних сообщений: «луна перескажи 50»
    "export": "экспорт",   # выгрузка истории в файл: «луна экспорт 100»
    "translate": "переведи",  # перевод последнего сообщения: «луна переведи на английский»
}

SUMMARY_DEFAULT = 50  # сколько сообщений брать без указания числа
SUMMARY_MAX = 100  # жёсткий потолок: больше ИИ переваривает плохо
EXPORT_MAX = 200  # потолок выгрузки истории

DEFAULT_CONFIG_FILE = str(BASE_DIR / "config.json")


def _cfg_bool(section: dict, key: str, default: bool, path: str) -> bool:
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    raise ValueError(f"{path}.{key} должно быть true/false")


def _cfg_number(section: dict, key: str, default, cast=float, minimum=0, path=""):
    try:
        value = cast(section.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}.{key} имеет неверное значение") from exc
    if value < minimum:
        raise ValueError(f"{path}.{key} должно быть не меньше {minimum}")
    return value


def _cfg_str_set(items) -> frozenset[str]:
    """Нормализует список ID/username в нижний регистр без @."""
    if not items:
        return frozenset()
    if isinstance(items, str):
        items = re.split(r"[,\s;]+", items)
    out: set[str] = set()
    for p in items:
        p = str(p).strip().lstrip("@").lower()
        if p:
            out.add(p)
    return frozenset(out)


def _cfg_str(section: dict, key: str, default: str, path: str) -> str:
    value = section.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{path}.{key} должно быть строкой")
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
    bot_name: str
    character: str
    trigger: str
    judge_trigger: str  # вычисляется: "<trigger> <COMMANDS['judge']>", в конфиге не задаётся
    summary_trigger: str  # вычисляется: "<trigger> <COMMANDS['summary']>"
    export_trigger: str  # вычисляется: "<trigger> <COMMANDS['export']>"
    translate_trigger: str  # вычисляется: "<trigger> <COMMANDS['translate']>"
    max_words: int
    self_reply: bool
    min_delay: float
    max_delay: float
    per_minute: int
    daily_limit: int
    session: str
    state_file: str
    debug: bool
    device_model: str | None = None
    device_system: str | None = None
    device_app: str | None = None
    device_lang: str | None = None
    chat_whitelist: frozenset = field(default_factory=frozenset)
    chat_blacklist: frozenset = field(default_factory=frozenset)
    user_whitelist: frozenset = field(default_factory=frozenset)
    ignore_channels: bool = True
    ignore_groups: bool = False
    search_enabled: bool = True
    search_max_results: int = 5
    tavily_key: str | None = None
    per_chat_per_minute: int = 3
    per_chat_daily: int = 50
    max_tokens: int = 4096

    @classmethod
    def load(cls, path: str | None = None):
        # Единственная env-переменная: где лежит конфиг. Всё остальное — в JSON.
        cfg_path = Path(path or os.getenv("CONFIG_FILE", DEFAULT_CONFIG_FILE))
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"Конфиг не найден: {cfg_path} (скопируй config.example.json)") from exc
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Конфиг {cfg_path} битый: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Конфиг {cfg_path}: корень должен быть объектом")

        tg = raw.get("telegram", {}) or {}
        ai = raw.get("ai", {}) or {}
        flt = raw.get("filters", {}) or {}
        sea = raw.get("search", {}) or {}
        lim = raw.get("limits", {}) or {}
        dev = raw.get("device", {}) or {}
        pth = raw.get("paths", {}) or {}

        try:
            api_id = int(str(tg.get("api_id", "")).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("telegram.api_id должно быть числом (my.telegram.org)") from exc
        api_hash = _cfg_str(tg, "api_hash", "", "telegram").strip()
        if api_id <= 0 or not api_hash or set(api_hash) == {"0"}:
            raise ValueError("telegram.api_id/api_hash не заполнены: укажи реальные данные с my.telegram.org")

        gemini = _cfg_str(ai, "gemini_key", "", "ai").strip() or None
        openai = _cfg_str(ai, "openai_key", "", "ai").strip() or None
        if not gemini and not openai:
            raise ValueError("Нужен ai.gemini_key или ai.openai_key в конфиге")
        for name, key in (("ai.gemini_key", gemini), ("ai.openai_key", openai)):
            if key and ("…" in key or any(ord(char) > 127 for char in key)):
                raise ValueError(f"{name} выглядит обрезанным: укажи полный ASCII-ключ без символа …")

        low = _cfg_number(lim, "min_delay_sec", 0.8, float, 0, "limits")
        high = _cfg_number(lim, "max_delay_sec", 2.5, float, 0, "limits")
        if high < low:
            raise ValueError("limits.max_delay_sec не может быть меньше limits.min_delay_sec")

        whitelist = _cfg_str_set(flt.get("whitelist", []))
        blacklist = _cfg_str_set(flt.get("blacklist", []))
        if whitelist and blacklist:
            raise ValueError("filters.whitelist и filters.blacklist взаимоисключающие: заполни только один")
        user_whitelist = _cfg_str_set(flt.get("user_whitelist", []))

        bot_name = _cfg_str(raw, "bot_name", "Луна", "").strip() or "Луна"
        character = _cfg_str(raw, "character", "luna-classic", "").strip() or "luna-classic"
        trigger = _cfg_str(raw, "trigger", "луна", "").strip().lower() or "луна"
        judge_trigger = f"{trigger} {COMMANDS['judge']}".strip()
        summary_trigger = f"{trigger} {COMMANDS['summary']}".strip()
        export_trigger = f"{trigger} {COMMANDS['export']}".strip()
        translate_trigger = f"{trigger} {COMMANDS['translate']}".strip()

        def _dev(key: str) -> str | None:
            v = _cfg_str(dev, key, "", "device").strip()
            return v or None

        return cls(
            api_id, api_hash, gemini, openai,
            _cfg_str(ai, "gemini_model", "gemini-2.5-flash", "ai").strip() or "gemini-2.5-flash",
            (_cfg_str(ai, "openai_base_url", "https://api.openai.com/v1", "ai").strip() or "https://api.openai.com/v1").rstrip("/"),
            _cfg_str(ai, "openai_model", "gpt-4o-mini", "ai").strip() or "gpt-4o-mini",
            bot_name, character, trigger, judge_trigger, summary_trigger, export_trigger, translate_trigger,
            _cfg_number(lim, "max_reply_words", 250, int, 1, "limits"),
            _cfg_bool(lim, "allow_self_reply", True, "limits"),
            low, high,
            _cfg_number(lim, "per_minute", 6, int, 1, "limits"),
            _cfg_number(lim, "daily", 200, int, 1, "limits"),
            _cfg_str(pth, "session", "data/luna_session", "paths"),
            _cfg_str(pth, "state", "data/luna_state.db", "paths"),
            _cfg_bool(raw, "debug", False, ""),
            _dev("model"), _dev("system"), _dev("app_version"), _dev("lang"),
            whitelist, blacklist, user_whitelist,
            _cfg_bool(flt, "ignore_channels", True, "filters"),
            _cfg_bool(flt, "ignore_groups", False, "filters"),
            _cfg_bool(sea, "enabled", True, "search"),
            _cfg_number(sea, "max_results", 5, int, 1, "search"),
            _cfg_str(sea, "tavily_key", "", "search").strip() or None,
            _cfg_number(lim, "per_chat_per_minute", 3, int, 1, "limits"),
            _cfg_number(lim, "per_chat_daily", 50, int, 1, "limits"),
            _cfg_number(lim, "max_tokens", 4096, int, 256, "limits"),
        )


# ------------------------------------------------------------------ helpers
def trim_words(text, limit):
    words = (text or "").strip().split()
    return " ".join(words) if len(words) <= limit else " ".join(words[:limit]) + "…"


def _trigger_end(text_lower: str, trigger: str) -> int:
    """Конец совпавшего триггера. Между словами триггера допускаются
    пробелы и пунктуация («луна, рассуди»). Возвращает -1 при несовпадении."""
    words = trigger.lower().strip().split()
    if not words:
        return -1
    pos = 0
    for i, w in enumerate(words):
        if not text_lower.startswith(w, pos):
            return -1
        pos += len(w)
        if i < len(words) - 1:
            j = pos
            while j < len(text_lower) and text_lower[j] in " \t\n\r,.:;!?—-\"'()[]{}":
                j += 1
            if j == pos:
                return -1  # слова триггера слиплись
            pos = j
    return pos


def is_trigger(text: str, trigger: str) -> bool:
    """Строго в начале сообщения. После триггера — конец/пробел/пунктуация."""
    if not text or not trigger:
        return False
    t = text.strip().lower()
    end = _trigger_end(t, trigger)
    if end < 0:
        return False
    if end >= len(t):
        return True
    return t[end] in " \t\n\r,.:;!?—-\"'()[]{}"


def extract_query(text: str, trigger: str) -> str:
    """Вытаскивает текст после триггера, съедая пунктуацию/пробелы слева."""
    raw = text.strip()
    end = _trigger_end(raw.lower(), trigger.lower().strip())
    if end < 0:
        return raw
    query = raw[end:]
    query = query.lstrip(" \t\n\r,.:;!?—-\"'()")
    return query.strip()


def _chat_keys(chat_id, chat=None) -> set[str]:
    """Все ключи чата для сверки со списками: id + username/title в нижнем регистре."""
    keys = {str(chat_id).lower()}
    for attr in ("username", "title"):
        try:
            v = getattr(chat, attr, None) if chat is not None else None
            if v and isinstance(v, str) and v.strip():
                keys.add(v.strip().lstrip("@").lower())
        except Exception:
            pass
    return keys


def chat_allowed(chat_id, cfg, chat=None, is_channel: bool = False, is_group: bool = False) -> tuple[bool, str]:
    """Проверка фильтров чата. Возвращает (разрешён, причина_блокировки)."""
    keys = _chat_keys(chat_id, chat)
    # whitelist и blacklist взаимоисключающие (проверяется в Config.load) —
    # активен ровно один режим: либо «только эти», либо «все кроме этих».
    if cfg.chat_whitelist:
        if keys & set(cfg.chat_whitelist):
            pass  # чат в белом списке — дальше проверки типа
        else:
            return False, "not-in-whitelist"
    elif cfg.chat_blacklist and keys & set(cfg.chat_blacklist):
        return False, "blacklist"
    if is_channel and cfg.ignore_channels:
        return False, "channel-ignored"
    if is_group and cfg.ignore_groups:
        return False, "group-ignored"
    return True, ""


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


# ------------------------------------------------------------------ web-поиск
# Поиск выполняется на КАЖДЫЙ запрос (если search.enabled=true): Луна всегда
# отвечает с опорой на актуальные данные, а не только на знания модели.
# Результаты кэшируются на SEARCH_CACHE_TTL секунд, чтобы не дублировать запросы.

_search_cache: dict[str, tuple[float, str]] = {}


async def _search_tavily(query: str, http: httpx.AsyncClient, api_key: str, limit: int) -> list[dict]:
    resp = await http.post("https://api.tavily.com/search",
                           headers={"Authorization": f"Bearer {api_key}"},
                           json={"query": query, "max_results": limit,
                                 "search_depth": "basic", "include_answer": True},
                           timeout=15.0)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for r in (data.get("results") or [])[:limit]:
        out.append({"title": r.get("title") or "", "url": r.get("url") or "",
                    "snippet": r.get("content") or ""})
    return out


async def _search_duckduckgo(query: str, http: httpx.AsyncClient, limit: int) -> list[dict]:
    """Поиск без API-ключа через html-интерфейс DuckDuckGo."""
    resp = await http.post("https://html.duckduckgo.com/html/",
                           data={"q": query},
                           headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                           timeout=15.0)
    resp.raise_for_status()
    page = resp.text
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', page, re.DOTALL)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.DOTALL)
    urls = re.findall(r'class="result__a"[^>]*href="([^"]+)', page)
    out = []
    for i, t in enumerate(titles[:limit]):
        title = html.unescape(re.sub(r"<.*?>", "", t)).strip()
        raw_url = urls[i] if i < len(urls) else ""
        # DDG заворачивает ссылку в редирект //duckduckgo.com/l/?uddg=<url>
        m = re.search(r"uddg=([^&]+)", raw_url)
        url = html.unescape(m.group(1)) if m else html.unescape(raw_url)
        try:
            from urllib.parse import unquote
            url = unquote(url)
        except Exception:
            pass
        snippet = html.unescape(re.sub(r"<.*?>", "", snippets[i])).strip() if i < len(snippets) else ""
        if title or snippet:
            out.append({"title": title, "url": url, "snippet": snippet})
    return out


async def web_search(query: str, http: httpx.AsyncClient, cfg, limit: int | None = None) -> str:
    """Возвращает текстовый блок свежих данных для подстановки в промпт (или пустую строку).
    Кэширует результаты на SEARCH_CACHE_TTL секунд."""
    limit = limit or cfg.search_max_results
    q = (query or "").strip()[:300]
    if not q:
        return ""
    cache_key = f"{q}:{limit}"
    now = time.time()
    if cache_key in _search_cache:
        ts, cached = _search_cache[cache_key]
        if now - ts < SEARCH_CACHE_TTL:
            return cached
    try:
        if cfg.tavily_key:
            results = await _search_tavily(q, http, cfg.tavily_key, limit)
        else:
            results = await _search_duckduckgo(q, http, limit)
    except Exception as exc:
        log.warning("Веб-поиск не удался (%s) — отвечаю без свежих данных", exc)
        return ""
    if not results:
        return ""
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    lines = [f"Актуальные данные из интернета (дата поиска: {today}). Учитывай их в ответе и ссылайся на них:"]
    for i, r in enumerate(results, 1):
        snippet = (r["snippet"] or "").strip()[:400]
        lines.append(f"{i}. {r['title']} — {snippet} ({r['url']})".strip())
    result = "\n".join(lines)
    _search_cache[cache_key] = (now, result)
    # ограничиваем рост кэша
    if len(_search_cache) > 100:
        oldest = min(_search_cache, key=lambda k: _search_cache[k][0])
        del _search_cache[oldest]
    return result


# ------------------------------------------------------------------ AI
class AI:
    def __init__(self, cfg):
        self.cfg = cfg
        # лимиты соединений + pooling
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), limits=limits, http2=False)
        # circuit breaker: после N подряд ошибок отказываемся на CIRCUIT_BREAKER_TIMEOUT сек
        self._failures = 0
        self._circuit_open_until = 0.0

    async def close(self):
        await self.http.aclose()

    def _circuit_check(self) -> bool:
        """True если запрос можно делать (цепь закрыта)."""
        if self._circuit_open_until and time.time() < self._circuit_open_until:
            return False
        self._circuit_open_until = 0.0
        return True

    def _circuit_success(self):
        self._failures = 0
        self._circuit_open_until = 0.0

    def _circuit_failure(self):
        self._failures += 1
        if self._failures >= CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_open_until = time.time() + CIRCUIT_BREAKER_TIMEOUT
            log.warning("Circuit breaker открыт на %ss (%s ошибок подряд)", CIRCUIT_BREAKER_TIMEOUT, self._failures)

    async def generate(self, prompt, system=None, images: list[tuple[bytes, str]] | None = None, token_limit=None):
        if not self._circuit_check():
            raise RuntimeError("AI временно недоступен (circuit breaker), повтори позже")
        system = system or SYSTEM_PROMPT
        output_tokens = min(token_limit or self.cfg.max_words * 3, self.cfg.max_tokens)
        images = images or []
        try:
            if self.cfg.gemini_key:
                result = await self._gemini(prompt, system, images, output_tokens)
            else:
                result = await self._openai(prompt, system, images, output_tokens)
            self._circuit_success()
            return result
        except Exception:
            self._circuit_failure()
            raise

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
        # per-chat лимиты: chat_id -> (times_deque, daily_count, day)
        self.chat_times: dict[str, deque] = {}
        self.chat_count: dict[str, int] = {}
        self.chat_day: dict[str, datetime.date] = {}
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
        self.db.execute("CREATE TABLE IF NOT EXISTS last_restart (id INTEGER PRIMARY KEY, ts REAL NOT NULL)")
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

    def chat_allowed(self, chat_id) -> bool:
        """Per-chat лимит: один спамер не блокирует остальные чаты."""
        key = str(chat_id)
        today = datetime.now().date()
        if self.chat_day.get(key) != today:
            self.chat_day[key] = today
            self.chat_count[key] = 0
        if self.chat_count[key] >= self.cfg.per_chat_daily:
            return False
        times = self.chat_times.setdefault(key, deque())
        cutoff = datetime.now() - timedelta(minutes=1)
        while times and times[0] <= cutoff:
            times.popleft()
        if len(times) >= self.cfg.per_chat_per_minute:
            return False
        return True

    def chat_register(self, chat_id):
        key = str(chat_id)
        self.chat_count[key] = self.chat_count.get(key, 0) + 1
        self.chat_times.setdefault(key, deque()).append(datetime.now())

    def get_last_restart(self) -> float:
        try:
            row = self.db.execute("SELECT ts FROM last_restart WHERE id=1").fetchone()
            return row[0] if row else 0.0
        except Exception:
            return 0.0

    def set_last_restart(self, ts: float):
        try:
            self.db.execute("INSERT OR REPLACE INTO last_restart(id, ts) VALUES (1, ?)", (ts,))
            self.db.commit()
        except Exception as exc:
            log.debug("set_last_restart: %s", exc)


def _get_reply_id(msg) -> int | None:
    """Надёжно достаёт reply_to_msg_id (Telethon хранит в reply_to)."""
    if msg is None:
        return None
    # основной путь — reply_to.reply_to_msg_id (MessageReplyHeader)
    rt = getattr(msg, "reply_to", None)
    if rt is not None:
        val = getattr(rt, "reply_to_msg_id", None)
        if val:
            return val
    # фолбек — прямой атрибут/свойство
    try:
        val = getattr(msg, "reply_to_msg_id", None)
        if val:
            return val
    except Exception:
        pass
    return None


def _is_from_bot(msg, bot_id: int) -> bool:
    """Проверка что сообщение от Луны (selfbot)."""
    if msg is None:
        return False
    if getattr(msg, "out", False) is True:
        # для selfbot все исходящие — от бота, но в Saved Messages и входящие тоже out=True,
        # поэтому дополнительно сверяем sender_id когда он есть
        sid = getattr(msg, "sender_id", None)
        if sid is None or sid == bot_id:
            return True
    sid = getattr(msg, "sender_id", None)
    if sid == bot_id:
        return True
    # фолбек через get_sender (редко нужен, но покрывает каналы)
    return False


async def _safe_get_sender_name(msg, fallback: str = "?") -> str:
    """Имя отправителя с защитой от ошибок (удалённый/недоступный пользователь)."""
    try:
        sender = await msg.get_sender()
        name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
        if name:
            return name
        return getattr(sender, "username", None) or fallback
    except Exception:
        return getattr(msg, "sender_id", None) or fallback


async def _download_images_parallel(msgs: list, client, limit: int = MAX_IMAGES_PER_REQUEST) -> list[tuple[bytes, str]]:
    """Параллельное скачивание картинок из списка сообщений."""
    tasks = []
    for m in msgs:
        if has_image(m):
            tasks.append(download_image(m, client))
            if len(tasks) >= limit:
                break
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, tuple):
            out.append(r)
    return out


# ------------------------------------------------------------------ thread context
async def collect_thread(event, telegram, bot_id: int, cfg) -> tuple[list, bool]:
    """Собирает цепочку по reply_to от корня до текущего.
    Условие треда: текущее сообщение — ответ на сообщение Луны (reply_to == bot)."""
    cur = event.message
    reply_to = _get_reply_id(cur)
    if not reply_to:
        log.debug("collect_thread: нет reply_to у %s (id=%s) — не тред", get_message_text(cur)[:40], cur.id)
        return [cur], False
    try:
        replied = await telegram.get_messages(event.chat_id, ids=reply_to)
        # Telethon может вернуть список если ids был списком — нормализуем
        if isinstance(replied, list):
            replied = replied[0] if replied else None
    except Exception as exc:
        log.debug("collect_thread: get_messages(%s) fail: %s", reply_to, exc)
        return [cur], False
    if not replied:
        log.debug("collect_thread: replied %s не найден", reply_to)
        return [cur], False
    if not _is_from_bot(replied, bot_id):
        # логируем кто реально отправитель для отладки
        try:
            s = await replied.get_sender()
            sid = getattr(s, "id", None)
        except Exception:
            sid = getattr(replied, "sender_id", None)
        log.debug("collect_thread: reply_to %s не от бота (sender_id=%s vs bot_id=%s) — не тред", reply_to, sid, bot_id)
        return [cur], False
    # в Избранном (Saved Messages) все сообщения от одного id, отличаем бота по отсутствию триггера
    # ответы Луны никогда не начинаются с кодового слова
    replied_text = get_message_text(replied)
    if is_trigger(replied_text, cfg.trigger) or is_trigger(replied_text, cfg.judge_trigger):
        log.debug("collect_thread: reply_to %s — это триггер-сообщение, а не ответ Луны — не тред", reply_to)
        return [cur], False

    chain: list = []
    visited: set[int] = set()
    depth = 0
    node = cur
    while node and depth < 30:
        if node.id in visited:
            log.debug("collect_thread: цикл на %s", node.id)
            break
        visited.add(node.id)
        chain.append(node)
        rid = _get_reply_id(node)
        if not rid:
            break
        try:
            prev = await telegram.get_messages(event.chat_id, ids=rid)
            if isinstance(prev, list):
                prev = prev[0] if prev else None
        except Exception as exc:
            log.debug("collect_thread: walk fail rid=%s: %s", rid, exc)
            break
        if not prev:
            log.debug("collect_thread: prev %s not found, обрыв", rid)
            break
        node = prev
        depth += 1

    chain.reverse()
    log.debug("collect_thread: собрано %s сообщений: ids=%s", len(chain), [m.id for m in chain])
    first_text = get_message_text(chain[0]) if chain else ""
    if not is_trigger(first_text, cfg.trigger):
        start_idx = None
        for i, m in enumerate(chain):
            if is_trigger(get_message_text(m), cfg.trigger):
                start_idx = i
                break
        if start_idx is not None:
            chain = chain[start_idx:]
            log.debug("collect_thread: обрезано с %s, осталось %s", start_idx, len(chain))
        else:
            log.debug("collect_thread: нет триггера в цепочке %s — не тред", [get_message_text(m)[:20] for m in chain])
            return [cur], False
    return chain, True


def format_thread_text(chain, bot_id: int) -> str:
    lines: list[str] = []
    for m in chain:
        txt = get_message_text(m).strip()
        suffix = " [изображение]" if has_image(m) else ""
        is_bot = _is_from_bot(m, bot_id)
        who = "Луна" if is_bot else f"Пользователь({getattr(m, 'sender_id', '?')})"
        ts = ""
        if getattr(m, "date", None):
            try:
                ts = m.date.astimezone().strftime("%H:%M")
            except Exception:
                ts = ""
        display = txt if txt else ("(изображение без текста)" if suffix else "(пусто)")
        lines.append(f"[{ts}] {who}: {display}{suffix}")
    return "\n".join(lines)


async def fetch_quoted(event, telegram, exclude_ids: set[int]) -> tuple[str, list] | tuple[None, list]:
    """Достаёт сообщение, на которое отвечают (reply), если его нет в истории треда.

    Возвращает (текстовый_блок | None, картинки). Нужно для вопросов вида
    «Луна, что думаешь об этом?» ответом на чужое сообщение.
    """
    rid = _get_reply_id(event.message)
    if not rid or rid in exclude_ids:
        return None, []
    try:
        quoted = await telegram.get_messages(event.chat_id, ids=rid)
        if isinstance(quoted, list):
            quoted = quoted[0] if quoted else None
    except Exception as exc:
        log.debug("fetch_quoted: get_messages(%s) fail: %s", rid, exc)
        return None, []
    if not quoted:
        return None, []
    txt = get_message_text(quoted).strip()
    if not txt and not has_image(quoted):
        return None, []
    try:
        sender = await quoted.get_sender()
        name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
        author = name or getattr(sender, "username", None) or getattr(quoted, "sender_id", "?")
    except Exception:
        author = getattr(quoted, "sender_id", "?")
    ts = ""
    try:
        if getattr(quoted, "date", None):
            ts = quoted.date.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    img_mark = " [изображение]" if has_image(quoted) else ""
    block = (f"Сообщение, на которое отвечает пользователь (цитата, id={rid}"
             + (f", {ts}" if ts else "") + f", автор: {author}):\n"
             + (txt or "(изображение без текста)") + img_mark)
    images: list = []
    if has_image(quoted) and len(images) < MAX_IMAGES_PER_REQUEST:
        dl = await download_image(quoted, telegram)
        if dl:
            images.append(dl)
    return block, images


async def answer_with_typing(event, telegram, ai, cfg, safety, prompt, images, system=None, token_limit=None, word_limit=None):
    """Единая точка ответа: typing + задержка + генерация + обрезка + отправка + учёт лимита."""
    delay = random.uniform(cfg.min_delay, cfg.max_delay)

    async def _gen():
        return await ai.generate(prompt, system or SYSTEM_PROMPT, images=images, token_limit=token_limit)

    try:
        async with telegram.action(event.chat_id, "typing"):
            await asyncio.sleep(delay)
            answer = await _gen()
    except Exception:
        # action может не поддерживаться в некоторых чатах — просто ждём
        await asyncio.sleep(delay)
        answer = await _gen()
    await event.reply(trim_words(answer, word_limit or cfg.max_words) or "Не удалось вынести вердикт.")
    safety.register()


def _parse_count(query: str, default: int = SUMMARY_DEFAULT, maximum: int = SUMMARY_MAX) -> int:
    """Первое число из запроса («перескажи 30»), clamp 1..maximum."""
    m = re.search(r"\d+", query or "")
    if not m:
        return default
    try:
        return max(1, min(int(m.group()), maximum))
    except ValueError:
        return default


async def summarize(event, telegram, ai, cfg, safety):
    """Выжимка переписки: последние N сообщений (до SUMMARY_MAX).

    Если команда написана ответом на сообщение — выжимка от него до команды.
    Иначе — последние N сообщений чата (N из текста команды или SUMMARY_DEFAULT).
    Текст + метки картинок, без скачивания медиа (экономия ресурсов).
    """
    if not safety.allowed():
        log.warning("Лимит ответов достигнут")
        return
    query = extract_query(get_message_text(event.message), cfg.summary_trigger)
    count = _parse_count(query)
    start_id = _get_reply_id(event.message)

    lines: list[str] = []
    if start_id:
        async for message in telegram.iter_messages(event.chat_id, min_id=start_id - 1, max_id=event.message.id, reverse=True):
            if message.id == event.message.id:
                continue
            txt = get_message_text(message)
            if not txt and not has_image(message):
                continue
            sender = await message.get_sender()
            name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
            timestamp = message.date.astimezone().strftime("%H:%M %d.%m") if message.date else "?"
            img_mark = " [изображение]" if has_image(message) else ""
            lines.append(f"[{timestamp}] {name or getattr(sender, 'username', None) or message.sender_id}: {(txt or '(изображение)').strip()}{img_mark}")
        # от ответа до команды может быть больше лимита — берём хвост
        lines = lines[-SUMMARY_MAX:]
    else:
        async for message in telegram.iter_messages(event.chat_id, limit=count + 1):
            if message.id == event.message.id:
                continue
            txt = get_message_text(message)
            if not txt and not has_image(message):
                continue
            sender = await message.get_sender()
            name = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
            timestamp = message.date.astimezone().strftime("%H:%M %d.%m") if message.date else "?"
            img_mark = " [изображение]" if has_image(message) else ""
            lines.append(f"[{timestamp}] {name or getattr(sender, 'username', None) or message.sender_id}: {(txt or '(изображение)').strip()}{img_mark}")
            if len(lines) >= count:
                break
        lines.reverse()

    if not lines:
        await event.reply("Тут пока пусто — пересказывать нечего.")
        return
    prompt = (
        f"Сделай краткую выжимку переписки (всего сообщений: {len(lines)}):\n\n"
        + "\n".join(lines)
        + "\n\nФормат: главные темы — что решили — кто что обещал или спрашивал. "
        "Коротко, по делу, по-русски. Имена участников сохраняй."
    )
    log.info("Саммари: сообщений %s (запрошено %s)", len(lines), count)
    await answer_with_typing(event, telegram, ai, cfg, safety, prompt, images=[], token_limit=2048, word_limit=500)


async def export_chat(event, telegram, ai, cfg, safety):
    """Выгрузка истории чата в текстовый файл: «луна экспорт 100»."""
    if not safety.allowed():
        log.warning("Лимит ответов достигнут")
        return
    query = extract_query(get_message_text(event.message), cfg.export_trigger)
    count = _parse_count(query, default=50, maximum=EXPORT_MAX)
    start_id = _get_reply_id(event.message)

    lines: list[str] = []
    if start_id:
        async for message in telegram.iter_messages(event.chat_id, min_id=start_id - 1, max_id=event.message.id, reverse=True):
            if message.id == event.message.id:
                continue
            txt = get_message_text(message)
            if not txt and not has_image(message):
                continue
            sender = await _safe_get_sender_name(message)
            timestamp = message.date.astimezone().strftime("%Y-%m-%d %H:%M") if message.date else "?"
            img_mark = " [изображение]" if has_image(message) else ""
            lines.append(f"[{timestamp}] {sender}: {(txt or '(изображение)').strip()}{img_mark}")
        lines = lines[-EXPORT_MAX:]
    else:
        async for message in telegram.iter_messages(event.chat_id, limit=count + 1):
            if message.id == event.message.id:
                continue
            txt = get_message_text(message)
            if not txt and not has_image(message):
                continue
            sender = await _safe_get_sender_name(message)
            timestamp = message.date.astimezone().strftime("%Y-%m-%d %H:%M") if message.date else "?"
            img_mark = " [изображение]" if has_image(message) else ""
            lines.append(f"[{timestamp}] {sender}: {(txt or '(изображение)').strip()}{img_mark}")
            if len(lines) >= count:
                break
        lines.reverse()

    if not lines:
        await event.reply("Тут пока пусто — экспортировать нечего.")
        return
    header = f"Экспорт чата {event.chat_id} — {len(lines)} сообщений\n{'='*40}\n"
    content = header + "\n".join(lines)
    file_path = Path(cfg.state_file).parent / f"export_{event.chat_id}_{int(time.time())}.txt"
    file_path.write_text(content, encoding="utf-8")
    log.info("Экспорт: %s сообщений -> %s", len(lines), file_path)
    await event.reply(f"Выгрузила {len(lines)} сообщений в файл.", file=str(file_path))


async def translate_last(event, telegram, ai, cfg, safety):
    """Перевод последнего чужого сообщения: «луна переведи на английский»."""
    if not safety.allowed():
        log.warning("Лимит ответов достигнут")
        return
    query = extract_query(get_message_text(event.message), cfg.translate_trigger)
    target = query or "английский"

    target_msg = None
    rid = _get_reply_id(event.message)
    if rid:
        try:
            target_msg = await telegram.get_messages(event.chat_id, ids=rid)
            if isinstance(target_msg, list):
                target_msg = target_msg[0] if target_msg else None
        except Exception:
            target_msg = None
    if not target_msg:
        async for message in telegram.iter_messages(event.chat_id, limit=20):
            if message.id == event.message.id:
                continue
            if _is_from_bot(message, (await telegram.get_me()).id):
                continue
            txt = get_message_text(message)
            if txt:
                target_msg = message
                break
    if not target_msg:
        await event.reply("Не нашла сообщение для перевода.")
        return
    src_text = get_message_text(target_msg).strip()
    if not src_text:
        await event.reply("В сообщении нет текста для перевода.")
        return
    prompt = f"Переведи следующий текст на язык «{target}». Ответь только переводом, без пояснений:\n\n{src_text[:2000]}"
    log.info("Перевод: %s символов на %s", len(src_text), target)
    await answer_with_typing(event, telegram, ai, cfg, safety, prompt, images=[], token_limit=1024, word_limit=300)


async def judge(event, telegram, ai, cfg, safety):
    start_id = _get_reply_id(event.message)
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
    context = "\n".join(lines)
    prompt = "Сообщения спора от начала до команды:\n\n" + context
    # Актуализация на каждый разбор: свежие данные поверх знаний модели
    trigger_query = extract_query(get_message_text(event.message), cfg.judge_trigger)
    if cfg.search_enabled:
        fresh = await web_search(trigger_query or context[-500:], ai.http, cfg)
        if fresh:
            prompt += "\n\n" + fresh
    await answer_with_typing(event, telegram, ai, cfg, safety, prompt, images, system=JUDGE_PROMPT, token_limit=4096)


# ------------------------------------------------------------------ глобальное состояние для горячей перезагрузки
_runtime = {
    "cfg": None,
    "system_prompt": None,
    "judge_prompt": None,
}


def reload_config(path: str | None = None):
    """Горячая перезагрузка конфига (SIGHUP). Вызывается из main()."""
    global SYSTEM_PROMPT, JUDGE_PROMPT
    try:
        new_cfg = Config.load(path)
        _runtime["cfg"] = new_cfg
        _runtime["system_prompt"] = _load_character(new_cfg.character)
        _runtime["judge_prompt"] = _load_prompt("judge.txt", FALLBACK_JUDGE)
        SYSTEM_PROMPT = _runtime["system_prompt"]
        JUDGE_PROMPT = _runtime["judge_prompt"]
        log.info("Конфиг перезагружён: trigger=%s character=%s", new_cfg.trigger, new_cfg.character)
        return True
    except Exception as exc:
        log.error("Ошибка перезагрузки конфига: %s (остаёмся на старом)", exc)
        return False


def clear_entity_cache(client):
    """Чистка entity cache Telethon для борьбы с ростом RAM."""
    try:
        client._entity_cache.clear()
        if hasattr(client, "_mb_entity_cache"):
            client._mb_entity_cache.clear()
        log.info("Entity cache очищен")
    except Exception as exc:
        log.debug("clear_entity_cache: %s", exc)


async def _periodic_maintenance(client, stop_event: asyncio.Event):
    """Фоновая задача: чистка entity cache + ротация search cache."""
    while not stop_event.is_set():
        try:
            await asyncio.sleep(ENTITY_CACHE_CLEAR_INTERVAL)
            clear_entity_cache(client)
            now = time.time()
            expired = [k for k, (ts, _) in _search_cache.items() if now - ts > SEARCH_CACHE_TTL]
            for k in expired:
                del _search_cache[k]
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.debug("maintenance: %s", exc)


async def main():
    try:
        cfg = Config.load()
    except ValueError as exc:
        logging.basicConfig(level=logging.INFO)
        log.error("Конфигурация: %s", exc)
        return
    logging.basicConfig(level=logging.DEBUG if cfg.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    _runtime["cfg"] = cfg
    _runtime["system_prompt"] = _load_character(cfg.character)
    _runtime["judge_prompt"] = _load_prompt("judge.txt", FALLBACK_JUDGE)
    SYSTEM_PROMPT = _runtime["system_prompt"]
    JUDGE_PROMPT = _runtime["judge_prompt"]

    ai, safety = AI(cfg), Safety(cfg)
    client_kwargs: dict = {}
    if cfg.device_model:
        client_kwargs["device_model"] = cfg.device_model
    if cfg.device_system:
        client_kwargs["system_version"] = cfg.device_system
    if cfg.device_app:
        client_kwargs["app_version"] = cfg.device_app
    if cfg.device_lang:
        client_kwargs["lang_code"] = cfg.device_lang
        client_kwargs.setdefault("system_lang_code", cfg.device_lang)
    telegram = TelegramClient(cfg.session, cfg.api_id, cfg.api_hash, **client_kwargs)
    started_at = datetime.now().astimezone()
    started_at_ts = started_at.timestamp()
    safety.set_last_restart(started_at_ts)
    last_restart_db = safety.get_last_restart()
    stop_event = asyncio.Event()
    maintenance_task = None
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

        def _handle_sigterm():
            log.info("Получен SIGTERM — останавливаемся")
            stop_event.set()

        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, _handle_sigterm)
        except (NotImplementedError, RuntimeError):
            pass
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, lambda: reload_config())
        except (NotImplementedError, RuntimeError):
            pass

        maintenance_task = asyncio.create_task(_periodic_maintenance(telegram, stop_event))

        @telegram.on(events.NewMessage())
        async def handler(event):
            try:
                cfg = _runtime["cfg"]
                msg = event.message
                # --- фильтры чата: дёшево, до любых сетевых вызовов ---
                try:
                    is_broadcast = bool(getattr(event, "is_channel", False)) and not bool(getattr(event, "is_group", False))
                    is_group = bool(getattr(event, "is_group", False))
                except Exception:
                    is_broadcast, is_group = False, False
                try:
                    chat_entity = getattr(event, "chat", None)
                except Exception:
                    chat_entity = None
                allowed, reason = chat_allowed(event.chat_id, cfg, chat_entity, is_broadcast, is_group)
                if not allowed:
                    log.debug("Игнор чата %s: %s", event.chat_id, reason)
                    return
                # --- фильтр пользователей ---
                try:
                    sender_preview = getattr(msg, "sender_id", None)
                except Exception:
                    sender_preview = None
                if cfg.user_whitelist and sender_preview:
                    sender_keys = {str(sender_preview).lower()}
                    try:
                        s = await event.get_sender()
                        uname = getattr(s, "username", None)
                        if uname:
                            sender_keys.add(uname.strip().lstrip("@").lower())
                    except Exception:
                        pass
                    if not (sender_keys & set(cfg.user_whitelist)):
                        log.debug("Игнор пользователя %s: not-in-whitelist", sender_preview)
                        return
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
                # дедуп с учётом рестарта: пропускаем сообщения до last_restart
                if last_restart_db and last_restart_db > 0:
                    try:
                        if msg.date.timestamp() <= last_restart_db and not safety.already_processed(event.chat_id, msg.id):
                            safety.already_processed(event.chat_id, msg.id)
                            return
                    except Exception:
                        pass
                if safety.already_processed(event.chat_id, msg.id):
                    return
                if not safety.chat_allowed(event.chat_id):
                    log.debug("Per-chat лимит в %s", event.chat_id)
                    return
                sender = await event.get_sender()
                if getattr(sender, "bot", False) or (event.out and not cfg.self_reply):
                    return

                if txt and is_trigger(txt, cfg.judge_trigger) and safety.allowed():
                    safety.chat_register(event.chat_id)
                    await judge(event, telegram, ai, cfg, safety)
                    return

                if txt and is_trigger(txt, cfg.summary_trigger) and safety.allowed():
                    safety.chat_register(event.chat_id)
                    await summarize(event, telegram, ai, cfg, safety)
                    return

                if txt and is_trigger(txt, cfg.export_trigger) and safety.allowed():
                    safety.chat_register(event.chat_id)
                    await export_chat(event, telegram, ai, cfg, safety)
                    return

                if txt and is_trigger(txt, cfg.translate_trigger) and safety.allowed():
                    safety.chat_register(event.chat_id)
                    await translate_last(event, telegram, ai, cfg, safety)
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
                chain_ids = {m.id for m in chain} if is_thread else {msg.id}
                quoted_block, quoted_images = await fetch_quoted(event, telegram, chain_ids)

                # --- веб-поиск на каждый запрос: актуальные данные поверх знаний модели ---
                fresh_block = ""
                if cfg.search_enabled and query:
                    fresh_block = await web_search(query, ai.http, cfg)

                images: list[tuple[bytes, str]] = list(quoted_images)
                if is_thread and len(chain) > 1:
                    thread_text = format_thread_text(chain[:-1], bot_id)
                    current_img_mark = " + изображение" if has_img else ""
                    parts = [f"Диалог (тред от первого обращения к Луне):\n{thread_text}"]
                    if quoted_block:
                        parts.append(quoted_block)
                    parts.append(
                        f"Новое сообщение от {sender_label} (@{username or 'нет'}){current_img_mark}:\n"
                        f"{query or '(только изображение, без текста)'}"
                    )
                    if fresh_block:
                        parts.append(fresh_block)
                    parts.append(
                        f"Ответь как Луна с учётом всего диалога выше. Не повторяй дословно историю, просто учти контекст. "
                        f"ВАЖНО: сейчас тебе пишет {sender_label} — отвечай ему и обращайся по имени ТОЛЬКО к нему. "
                        f"Имена из диалога выше — это история, к тем людям не обращайся."
                    )
                    prompt = "\n\n".join(parts)
                    img_msgs = [m for m in chain if has_image(m)]
                    if img_msgs:
                        images.extend(await _download_images_parallel(img_msgs, telegram))
                    log.info("Тред-запрос от %s: %s (цепочка %s, картинок %s, поиск=%s)",
                             sender_label, (query or "[img]")[:100], len(chain), len(images), bool(fresh_block))
                    safety.chat_register(event.chat_id)
                    await answer_with_typing(event, telegram, ai, cfg, safety, prompt, images)
                else:
                    if has_img:
                        dl = await download_image(msg, telegram)
                        if dl:
                            images.append(dl)
                    request_time = msg.date.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
                    img_note = "\n[К сообщению приложено изображение — опиши/учти его]" if images else ""
                    parts = [
                        f"Отправитель: {sender_label}\n"
                        f"Username: @{username if username else 'нет'}\n"
                        f"Время: {request_time}{img_note}",
                    ]
                    if quoted_block:
                        parts.append(quoted_block)
                    parts.append(f"Запрос: {query or '(пользователь прислал изображение без текста — опиши что на нём и ответь по-человечески)'}")
                    if fresh_block:
                        parts.append(fresh_block)
                    parts.append(f"Отвечай отправителю ({sender_label}) — обращайся по имени ТОЛЬКО к нему.")
                    prompt = "\n\n".join(parts)
                    log.info("Запрос от %s в %s: %s%s%s", sender_label, request_time,
                             (query or "[изображение]")[:100],
                             " +img" if images else "",
                             " +web" if fresh_block else "")
                    safety.chat_register(event.chat_id)
                    await answer_with_typing(event, telegram, ai, cfg, safety, prompt, images)

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

        log.info("Луна слушает: %s; разбор: %s; выжимка: %s; экспорт: %s; перевод: %s (характер: %s)",
                 cfg.trigger, cfg.judge_trigger, cfg.summary_trigger, cfg.export_trigger, cfg.translate_trigger, cfg.character)
        log.info("Фильтры: whitelist=%s blacklist=%s user_whitelist=%s ignore_channels=%s ignore_groups=%s",
                 sorted(cfg.chat_whitelist) or "—", sorted(cfg.chat_blacklist) or "—",
                 sorted(cfg.user_whitelist) or "—", cfg.ignore_channels, cfg.ignore_groups)
        log.info("Веб-поиск: %s (результатов: %s, tavily=%s, кэш=%ss)",
                 "вкл" if cfg.search_enabled else "выкл",
                 cfg.search_max_results, "да" if cfg.tavily_key else "нет", SEARCH_CACHE_TTL)
        await telegram.run_until_disconnected()
    finally:
        stop_event.set()
        if maintenance_task:
            maintenance_task.cancel()
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
