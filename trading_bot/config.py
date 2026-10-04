"""Environment configuration and explicit sandbox-only safety guards."""

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    ticker: str = "SBER"
    sandbox: bool = True
    enable_sandbox_orders: bool = False
    sandbox_initial_balance: Decimal = Decimal("100000")
    enable_auto_trading: bool = False


def read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false.")
    return value == "true"


def load_settings() -> Settings:
    env_path = Path(
        os.getenv("TRADING_ENV_FILE", str(Path(__file__).with_name(".env")))
    )
    load_dotenv(env_path, override=False)
    sandbox = read_bool("SANDBOX", True)
    if not sandbox:
        raise RuntimeError("Real trading is disabled in this demo.")
    token = os.getenv("T_INVEST_TOKEN", "").strip()
    if not token:
        raise ValueError("Set T_INVEST_TOKEN in trading_bot/.env (sandbox token).")
    ticker = os.getenv("TICKER", "SBER").strip().upper()
    if not ticker:
        raise ValueError("TICKER cannot be empty.")
    try:
        balance = Decimal(os.getenv("SANDBOX_INITIAL_BALANCE", "100000"))
    except InvalidOperation as exc:
        raise ValueError("SANDBOX_INITIAL_BALANCE must be a number.") from exc
    if not balance.is_finite() or balance <= 0 or balance > Decimal("1000000000"):
        raise ValueError("SANDBOX_INITIAL_BALANCE must be > 0 and <= 1000000000.")
    if balance != balance.quantize(Decimal("0.000000001")):
        raise ValueError("SANDBOX_INITIAL_BALANCE supports at most 9 decimal places.")
    return Settings(
        token,
        ticker,
        sandbox,
        read_bool("ENABLE_SANDBOX_ORDERS", False),
        balance,
        read_bool("ENABLE_AUTO_TRADING", False),
    )


def ensure_sandbox_trading_allowed(settings: Settings) -> None:
    if not settings.sandbox:
        raise RuntimeError("Real trading is disabled in this demo.")
    if not settings.enable_sandbox_orders:
        raise RuntimeError("Sandbox order execution is disabled.")


def ensure_auto_trading_allowed(settings: Settings) -> None:
    ensure_sandbox_trading_allowed(settings)
    if not settings.enable_auto_trading:
        raise RuntimeError("Set ENABLE_AUTO_TRADING=true to execute sandbox strategy.")
