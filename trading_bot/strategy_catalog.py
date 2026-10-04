"""Declarative strategy profiles: importing JSON never executes code."""

import json
import math
import os
import tempfile
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4
from strategy import StrategyParams

ROOT = Path(__file__).parent
CATALOG = ROOT / "strategy_profiles"
RUNTIME = ROOT / ".runtime"


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    ticker: str
    params: StrategyParams = field(default_factory=StrategyParams)
    initial: float = 100000
    fee_bps: float = 5
    slippage_bps: float = 5

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.id):
            raise ValueError(
                "ID: латинские строчные буквы, цифры, _ и -, до 64 символов."
            )
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or len(self.name) > 100
        ):
            raise ValueError("Название: 1–100 символов.")
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,23}", self.ticker):
            raise ValueError("Некорректный тикер.")
        if not math.isfinite(self.initial) or not 100 <= self.initial <= 1e9:
            raise ValueError("Виртуальный капитал: 100..1000000000 ₽.")
        if not all(
            math.isfinite(v) and 0 <= v <= 100
            for v in [self.fee_bps, self.slippage_bps]
        ):
            raise ValueError("Комиссия и проскальзывание: 0..100 bps.")

    @property
    def state_path(self):
        return RUNTIME / self.id / "bot.json"

    @property
    def account_name(self):
        return "trading-profile-" + self.id

    def public(self):
        return asdict(self)


def parse_profile(data):
    if not isinstance(data, dict) or set(data) - {
        "id",
        "name",
        "ticker",
        "params",
        "initial",
        "fee_bps",
        "slippage_bps",
    }:
        raise ValueError(
            "Профиль должен быть JSON-объектом с полями id, name, ticker, params, initial, fee_bps, slippage_bps."
        )
    try:
        return Profile(
            str(data.get("id") or uuid4().hex[:12]),
            data["name"],
            str(data["ticker"]).strip().upper(),
            StrategyParams(**data.get("params", {})),
            float(data.get("initial", 100000)),
            float(data.get("fee_bps", 5)),
            float(data.get("slippage_bps", 5)),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("Неверный формат профиля или параметров стратегии.") from exc


def profiles():
    result = {}
    for path in sorted(CATALOG.glob("*.json")):
        profile = parse_profile(json.loads(path.read_text()))
        if profile.id in result:
            raise ValueError("Повторяющийся ID профиля: " + profile.id)
        result[profile.id] = profile
    return result


def get_profile(identifier):
    try:
        return profiles()[identifier]
    except KeyError as exc:
        raise ValueError("Неизвестный профиль стратегии.") from exc


def import_profile(data):
    profile = parse_profile(data)
    CATALOG.mkdir(exist_ok=True)
    if profile.id in profiles():
        raise ValueError(
            "Такой ID уже существует. Для другого варианта используйте новый ID."
        )
    # Publish a complete file exclusively; polling never sees a partial profile.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=CATALOG, suffix=".tmp", delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(
                profile.public(), file, ensure_ascii=False, indent=2, allow_nan=False
            )
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, CATALOG / (profile.id + ".json"))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return profile
