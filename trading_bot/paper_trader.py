"""Persistent virtual fills on current quotes; no order APIs are used."""

import copy
import hashlib
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from strategy import configured_signal
from research_io import atomic_text

MSK = ZoneInfo("Europe/Moscow")


class PaperSession:
    def __init__(self, profile):
        self.profile = profile
        self.path = profile.state_path.with_suffix(".paper.json")
        fingerprint = hashlib.sha256(
            json.dumps(profile.public(), sort_keys=True).encode()
        ).hexdigest()
        self.state = {
            "fingerprint": fingerprint,
            "cash": profile.initial,
            "lots": 0,
            "entry_cost": 0.0,
            "entry_price": 0.0,
            "lot_size": 0,
            "fees": 0.0,
            "realized": 0.0,
            "mark": 0.0,
            "peak": profile.initial,
            "drawdown_pct": 0.0,
            "last_bar": None,
            "last_quote": None,
            "day": None,
            "day_equity": profile.initial,
            "entries": 0,
            "attempts": 0,
            "halted": False,
            "events": [],
            "curve": [],
            "decision": "Остановлен",
        }
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            if not isinstance(saved, dict):
                raise ValueError("Повреждено состояние бумажной торговли.")
            if saved.get("fingerprint") != fingerprint:
                raise ValueError(
                    "Параметры профиля изменены. Создайте профиль с новым ID для отдельной симуляции."
                )
            # Fail closed rather than silently resetting an existing portfolio.
            if (
                set(saved) != set(self.state)
                or type(saved["lots"]) is not int
                or saved["lots"] not in (0, 1)
                or not all(
                    type(saved[k]) in (int, float)
                    and math.isfinite(saved[k])
                    and saved[k] >= 0
                    for k in [
                        "cash",
                        "fees",
                        "entry_cost",
                        "entry_price",
                        "mark",
                        "peak",
                        "day_equity",
                        "drawdown_pct",
                    ]
                )
                or type(saved["realized"]) not in (int, float)
                or not math.isfinite(saved["realized"])
                or saved["peak"] <= 0
                or not all(
                    type(saved[k]) is int and saved[k] >= 0
                    for k in ["lot_size", "entries", "attempts"]
                )
                or (saved["lots"] and saved["lot_size"] < 1)
                or type(saved["halted"]) is not bool
                or not isinstance(saved["decision"], str)
                or not self.valid_records(
                    saved["events"], 200, ("price", "shares", "fee")
                )
                or not self.valid_records(saved["curve"], 500, ("equity",))
                or not all(
                    self.valid_time(saved[k]) for k in ["last_bar", "last_quote"]
                )
            ):
                raise ValueError("Повреждено состояние бумажной торговли.")
            self.state = saved
        self.running = False

    @staticmethod
    def valid_time(value):
        if value is None:
            return True
        try:
            return datetime.fromisoformat(value).utcoffset() is not None
        except (ValueError, TypeError):
            return False

    @classmethod
    def valid_records(cls, records, limit, fields):
        return (
            isinstance(records, list)
            and len(records) <= limit
            and all(
                isinstance(row, dict)
                and row.get("time") is not None
                and cls.valid_time(row["time"])
                and all(
                    type(row.get(k)) in (int, float)
                    and math.isfinite(row[k])
                    and row[k] >= 0
                    for k in fields
                )
                for row in records
            )
        )

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(
            self.path, json.dumps(self.state, ensure_ascii=False, allow_nan=False)
        )

    def start(self, snapshot):
        if (
            not snapshot.get("connected")
            or snapshot.get("ticker") != self.profile.ticker
        ):
            raise ValueError("Дождитесь актуальных данных выбранного инструмента.")
        bars = snapshot.get("series") or []
        if self.state["last_bar"] is None and bars:
            self.state["last_bar"] = bars[-1]["time"]
        self.state["decision"] = "Ожидаем следующую закрытую свечу"
        self.save()
        self.running = True

    def stop(self):
        self.running = False
        self.state["decision"] = "Приостановлен; виртуальная позиция сохранена"
        self.save()

    def snapshot(self, lot):
        s = copy.deepcopy(self.state)
        s.update(running=self.running, initial=self.profile.initial)
        lot = s["lot_size"] or lot
        s["equity"] = s["cash"] + s["lots"] * lot * s["mark"]
        s["net_pnl"] = s["equity"] - self.profile.initial
        s["unrealized"] = (
            s["lots"] * lot * s["mark"] - s["entry_cost"] if s["lots"] else 0.0
        )
        return s

    def update(self, data):
        if not self.running:
            return
        s = self.state
        if not data.get("connected") or data.get("ticker") != self.profile.ticker:
            s["decision"] = "Ожидаем связь с выбранным инструментом"
            return
        price, age, lot = data.get("price"), data.get("quote_age"), data.get("lot")
        if (
            not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price <= 0
            or not isinstance(age, (int, float))
            or not 0 <= age <= 120
            or not isinstance(lot, int)
            or lot < 1
            or data.get("currency") != "rub"
        ):
            s["decision"] = "Ожидаем свежую котировку рублёвого инструмента"
            return
        market = data.get("market") or {}
        if not market.get("api") or not market.get("market"):
            s["decision"] = "Торги недоступны; виртуальные сделки не моделируются"
            return
        quote = data["quote_time"]
        if s["last_quote"] and datetime.fromisoformat(quote) <= datetime.fromisoformat(
            s["last_quote"]
        ):
            return
        s["last_quote"], s["mark"], s["lot_size"] = quote, price, lot
        equity = s["cash"] + s["lots"] * lot * price
        day = datetime.fromisoformat(quote).astimezone(MSK).date().isoformat()
        if day != s["day"]:
            s.update(day=day, day_equity=equity, entries=0, attempts=0, halted=False)
        if equity <= s["day_equity"] * 0.99:
            s["halted"] = True
        bars = data.get("series") or []
        bar = bars[-1]["time"] if bars else None
        new_bar = bar and (
            s["last_bar"] is None
            or datetime.fromisoformat(bar) > datetime.fromisoformat(s["last_bar"])
        )
        signal = "HOLD"
        if new_bar:
            s["last_bar"] = bar
            if (
                datetime.fromisoformat(quote)
                >= datetime.fromisoformat(bar) + timedelta(minutes=15)
                and 0
                <= (
                    datetime.fromisoformat(data["updated"])
                    - datetime.fromisoformat(bar)
                ).total_seconds()
                <= 1800
            ):
                signal = configured_signal(
                    [b["close"] for b in bars], self.profile.params
                )
        side, reason = "HOLD", "Нет нового сигнала"
        if s["lots"] and (
            s["halted"]
            or price <= s["entry_price"] * (1 - self.profile.params.stop_pct / 100)
            or signal == "SELL"
        ):
            side, reason = "SELL", "Выход: стоп / дневной риск / сигнал"
        elif not s["lots"] and not s["halted"] and signal == "BUY":
            side, reason = "BUY", "Сигнал входа"
        slip, fee = self.profile.slippage_bps / 10000, self.profile.fee_bps / 10000
        fill = price * (1 + slip if side == "BUY" else 1 - slip)
        gross, commission = fill * lot, fill * lot * fee
        if side == "BUY" and (
            s["entries"] >= 2 or gross + commission > min(s["cash"], equity * 0.05)
        ):
            side, reason = (
                "HOLD",
                "Вход ограничен: максимум 2 входа и 5% капитала на 1 лот",
            )
        if side != "HOLD" and s["attempts"] >= 4:
            side, reason = "HOLD", "Достигнут дневной лимит виртуальных сделок"
        if side != "HOLD":
            s["attempts"] += 1
            s["fees"] += commission
            if side == "BUY":
                s["cash"] -= gross + commission
                s.update(lots=1, entry_price=fill, entry_cost=gross + commission)
                s["entries"] += 1
            else:
                s["cash"] += gross - commission
                s["realized"] += gross - commission - s["entry_cost"]
                s.update(lots=0, entry_price=0.0, entry_cost=0.0)
            s["events"].append(
                {
                    "time": quote,
                    "action": side,
                    "price": fill,
                    "shares": lot,
                    "fee": commission,
                    "reason": reason,
                }
            )
            s["events"] = s["events"][-200:]
        s["decision"] = reason
        equity = s["cash"] + s["lots"] * lot * price
        s["peak"] = max(s["peak"], equity)
        s["drawdown_pct"] = max(
            s["drawdown_pct"], (s["peak"] - equity) / s["peak"] * 100
        )
        s["curve"].append({"time": quote, "equity": equity})
        s["curve"] = s["curve"][-500:]
        self.save()
