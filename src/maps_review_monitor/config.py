from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class ShopConfig:
    name: str
    url: str
    enabled: bool = True

    @property
    def key(self) -> str:
        import hashlib
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    timezone: str
    locale: str
    scan_limit: int
    known_streak_stop: int
    headless: bool
    browser_channel: str
    browser_executable: str
    browser_profile_dir: Path
    navigation_timeout_seconds: int
    shop_delay_min_seconds: float
    shop_delay_max_seconds: float
    profile_delay_min_seconds: float
    profile_delay_max_seconds: float
    scroll_delay_min_seconds: float
    scroll_delay_max_seconds: float
    telegram_send_delay_min_seconds: float
    telegram_send_delay_max_seconds: float
    telegram_batch_limit: int
    data_dir: Path
    log_dir: Path
    debug_dir: Path
    shops: tuple[ShopConfig, ...]
    telegram_bot_token: str
    telegram_chat_id: str

    @property
    def database_path(self) -> Path:
        return self.data_dir / "reviews.sqlite3"


def load_settings(config_path: str | Path = "config.toml", require_telegram: bool = False) -> Settings:
    path = Path(config_path).resolve()
    if not path.exists():
        raise ValueError(f"找不到設定檔：{path}（請先複製 config.example.toml）")
    load_dotenv(path.parent / ".env")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    shops = tuple(ShopConfig(**item) for item in raw.get("shops", []) if item.get("enabled", True))
    if not shops:
        raise ValueError("config.toml 至少需要一個 enabled=true 的 [[shops]]")
    if len(shops) > 10:
        raise ValueError("目前設計最多支援 10 家店")
    for shop in shops:
        parsed = urlparse(shop.url)
        if parsed.scheme != "https" or "google." not in parsed.netloc:
            raise ValueError(f"店家網址看起來不是 Google Maps HTTPS 網址：{shop.name}")
    def rel(name: str, default: str) -> Path:
        value = Path(raw.get(name, default))
        return value if value.is_absolute() else path.parent / value
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if require_telegram and (not token or not chat_id):
        raise ValueError(".env 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    scan_limit = int(raw.get("scan_limit", 50))
    streak = int(raw.get("known_streak_stop", 10))
    if not 1 <= scan_limit <= 200 or not 1 <= streak <= scan_limit:
        raise ValueError("scan_limit 必須是 1..200，known_streak_stop 必須介於 1 與 scan_limit")
    def delay_range(name: str, default_min: float, default_max: float) -> tuple[float, float]:
        minimum = float(raw.get(f"{name}_delay_min_seconds", default_min))
        maximum = float(raw.get(f"{name}_delay_max_seconds", default_max))
        if minimum < 0 or maximum < minimum:
            raise ValueError(f"{name}_delay_min_seconds 必須 >= 0，且 max 必須 >= min")
        return minimum, maximum
    shop_delay = delay_range("shop", 20, 45)
    profile_delay = delay_range("profile", 4, 10)
    scroll_delay = delay_range("scroll", 1.5, 3)
    telegram_delay = delay_range("telegram_send", 3, 7)
    telegram_batch_limit = int(raw.get("telegram_batch_limit", 15))
    if not 1 <= telegram_batch_limit <= 100:
        raise ValueError("telegram_batch_limit 必須介於 1 與 100")
    return Settings(
        root=path.parent, timezone=str(raw.get("timezone", "Asia/Taipei")),
        locale=str(raw.get("locale", "zh-TW")), scan_limit=scan_limit,
        known_streak_stop=streak, headless=bool(raw.get("headless", True)),
        browser_channel=str(raw.get("browser_channel", "")),
        browser_executable=str(raw.get("browser_executable", "")),
        browser_profile_dir=rel("browser_profile_dir", "data/chromium-profile"),
        navigation_timeout_seconds=int(raw.get("navigation_timeout_seconds", 45)),
        shop_delay_min_seconds=shop_delay[0], shop_delay_max_seconds=shop_delay[1],
        profile_delay_min_seconds=profile_delay[0], profile_delay_max_seconds=profile_delay[1],
        scroll_delay_min_seconds=scroll_delay[0], scroll_delay_max_seconds=scroll_delay[1],
        telegram_send_delay_min_seconds=telegram_delay[0],
        telegram_send_delay_max_seconds=telegram_delay[1],
        telegram_batch_limit=telegram_batch_limit,
        data_dir=rel("data_dir", "data"), log_dir=rel("log_dir", "logs"),
        debug_dir=rel("debug_dir", "debug"), shops=shops,
        telegram_bot_token=token, telegram_chat_id=chat_id,
    )
