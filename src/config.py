import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
CHAT_ID: int | None = int(os.getenv("TELEGRAM_CHAT_ID")) if os.getenv("TELEGRAM_CHAT_ID") else None

TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Los_Angeles"))

MORNING_PING_TIME: str = os.getenv("MORNING_PING_TIME", "07:00")
EVENING_PING_TIME: str = os.getenv("EVENING_PING_TIME", "21:00")
DEFAULT_TIMER_MINUTES: int = int(os.getenv("DEFAULT_TIMER_MINUTES", "25"))
SNOOZE_MINUTES: int = int(os.getenv("SNOOZE_MINUTES", "15"))
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH: str = os.getenv("DB_PATH", os.path.join(_SRC_DIR, "..", "data", "adhd.db"))


def morning_time() -> tuple[int, int]:
    h, m = MORNING_PING_TIME.split(":")
    return int(h), int(m)


def evening_time() -> tuple[int, int]:
    h, m = EVENING_PING_TIME.split(":")
    return int(h), int(m)
