"""Разовая миграция .env -> config.json. Запуск: python tools/migrate_env_to_json.py [.env] [config.json]"""
import json
import sys
from pathlib import Path

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".env")
DST = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("config.json")
EXAMPLE = Path("config.example.json")


def parse_env(path: Path) -> dict:
    out: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("\"'")
    return out


def to_bool(v: str, default: bool) -> bool:
    return default if v == "" else v.lower() in {"1", "true", "yes", "on"}


def main() -> None:
    if DST.exists():
        print(f"ОТМЕНА: {DST} уже существует — удали его или укажи другой путь.")
        sys.exit(1)
    env = parse_env(SRC) if SRC.exists() else {}
    cfg = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    cfg["trigger"] = env.get("TRIGGER_WORD", cfg["trigger"])
    cfg["telegram"]["api_id"] = int(env.get("API_ID", 0) or 0)
    cfg["telegram"]["api_hash"] = env.get("API_HASH", "")
    cfg["ai"]["gemini_key"] = env.get("GEMINI_API_KEY", "")
    cfg["ai"]["gemini_model"] = env.get("GEMINI_MODEL", cfg["ai"]["gemini_model"])
    cfg["ai"]["openai_key"] = env.get("OPENAI_API_KEY", "")
    cfg["ai"]["openai_base_url"] = env.get("OPENAI_BASE_URL", cfg["ai"]["openai_base_url"])
    cfg["ai"]["openai_model"] = env.get("OPENAI_MODEL", cfg["ai"]["openai_model"])
    lim = cfg["limits"]
    lim["max_reply_words"] = int(env.get("MAX_REPLY_WORDS", lim["max_reply_words"]))
    lim["allow_self_reply"] = to_bool(env.get("ALLOW_SELF_REPLY", ""), True)
    lim["min_delay_sec"] = float(env.get("MIN_DELAY_SEC", lim["min_delay_sec"]))
    lim["max_delay_sec"] = float(env.get("MAX_DELAY_SEC", lim["max_delay_sec"]))
    lim["per_minute"] = int(env.get("MAX_REPLIES_PER_MIN", lim["per_minute"]))
    lim["daily"] = int(env.get("DAILY_REPLY_LIMIT", lim["daily"]))
    flt = cfg["filters"]
    flt["whitelist"] = [x for x in env.get("CHAT_WHITELIST", "").replace(",", " ").split() if x]
    flt["blacklist"] = [x for x in env.get("CHAT_BLACKLIST", "").replace(",", " ").split() if x]
    flt["ignore_channels"] = to_bool(env.get("IGNORE_CHANNELS", ""), True)
    flt["ignore_groups"] = to_bool(env.get("IGNORE_GROUPS", ""), False)
    sea = cfg["search"]
    sea["enabled"] = to_bool(env.get("SEARCH_ENABLED", ""), True)
    sea["max_results"] = int(env.get("SEARCH_MAX_RESULTS", sea["max_results"]))
    sea["tavily_key"] = env.get("TAVILY_API_KEY", "")
    if env.get("SESSION_FILE"):
        cfg["paths"]["session"] = env["SESSION_FILE"]
    if env.get("STATE_FILE"):
        cfg["paths"]["state"] = env["STATE_FILE"]
    cfg["debug"] = to_bool(env.get("DEBUG", ""), False)
    # JUDGE_TRIGGER из .env больше не используется: команда «рассуди» живёт в коде (COMMANDS)

    DST.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Готово: {SRC} -> {DST}. Проверь ключи и удали .env.")


if __name__ == "__main__":
    main()
