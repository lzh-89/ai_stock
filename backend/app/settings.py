from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"


def _load_json_config() -> Dict[str, Any]:
    config_path = PROJECT_ROOT / "config" / "settings.json"
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as file:
        return json.load(file)


@dataclass(frozen=True)
class Settings:
    market_universe: tuple[str, ...]
    include_bse: bool
    include_star_market: bool
    refresh_minutes: int
    kline_period: str
    database_path: Path
    ai_provider: str
    ai_api_key: str
    ai_model: str
    ai_base_url: str
    enable_ai: bool
    tushare_token: str
    stock_case_prompt: Dict[str, Any]

    def ai_mode(self, requested: bool = True) -> str:
        if requested and self.enable_ai and self.ai_api_key and self.ai_base_url:
            return self.ai_provider
        return "rules"


def load_settings() -> Settings:
    _load_env_file()
    config = _load_json_config()
    database_path = Path(config.get("database_path", "data/app.sqlite"))
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path

    ai_provider = _resolve_ai_provider(config)
    ai_api_key = _resolve_ai_api_key(ai_provider)
    ai_model = _resolve_ai_model(config, ai_provider)
    ai_base_url = _resolve_ai_base_url(config, ai_provider)

    return Settings(
        market_universe=tuple(config.get("market_universe", ["main_board", "chinext"])),
        include_bse=bool(config.get("include_bse", False)),
        include_star_market=bool(config.get("include_star_market", False)),
        refresh_minutes=int(config.get("refresh_minutes", 15)),
        kline_period=str(config.get("kline_period", "15m")),
        database_path=database_path,
        ai_provider=ai_provider,
        ai_api_key=ai_api_key,
        ai_model=ai_model,
        ai_base_url=ai_base_url,
        enable_ai=os.getenv("ENABLE_AI", os.getenv("ENABLE_OPENAI", "true")).lower() not in {"0", "false", "no"},
        tushare_token=os.getenv("TUSHARE_TOKEN", ""),
        stock_case_prompt=dict(config.get("stock_case_prompt") or {}),
    )


def _resolve_ai_provider(config: Dict[str, Any]) -> str:
    requested = os.getenv("AI_PROVIDER", str(config.get("ai_provider", ""))).strip().lower()
    if requested:
        return requested
    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return str(config.get("default_ai_provider", "deepseek")).strip().lower() or "deepseek"


def _resolve_ai_api_key(provider: str) -> str:
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_API_KEY", os.getenv("AI_API_KEY", ""))
    if provider == "openai":
        return os.getenv("OPENAI_API_KEY", os.getenv("AI_API_KEY", ""))
    return os.getenv("AI_API_KEY", os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENAI_API_KEY", "")))


def _resolve_ai_model(config: Dict[str, Any], provider: str) -> str:
    configured = os.getenv("AI_MODEL", str(config.get("ai_model", ""))).strip()
    if configured:
        return configured
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_MODEL", str(config.get("deepseek_model", "deepseek-v4-flash")))
    if provider == "openai":
        return os.getenv("OPENAI_MODEL", str(config.get("openai_model", "gpt-5.4-mini")))
    return str(config.get("custom_ai_model", ""))


def _resolve_ai_base_url(config: Dict[str, Any], provider: str) -> str:
    configured = os.getenv("AI_BASE_URL", str(config.get("ai_base_url", ""))).strip()
    if configured:
        return configured.rstrip("/")
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if provider == "openai":
        return "https://api.openai.com/v1"
    return ""


def _load_env_file() -> None:
    if not ENV_PATH.exists():
        return
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, _strip_env_value(value.strip()))


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
