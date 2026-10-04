"""Local-only dashboard: cached API data and an owned bot subprocess."""

import json
import math
from uuid import uuid4
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from urllib.parse import parse_qs, urlsplit
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import mean

if sys.platform == "darwin":
    os.environ.setdefault("GRPC_DNS_RESOLVER", "native")

from t_tech.invest import CandleInterval
from auto_trader import ACCOUNT_NAME, STATE_PATH, state_lock
from config import ensure_auto_trading_allowed, load_settings
from log_setup import LOG_PATH, RedactTokens, configure_logging
from strategy import configured_signal
from strategy_catalog import profiles, get_profile, import_profile
from paper_trader import PaperSession
from journal import database
from t_invest_client import SandboxBroker, quotation_to_float

ROOT = Path(__file__).parent
logger = logging.getLogger(__name__)


def safe_message(error: BaseException) -> str:
    message = str(error)
    record = logging.LogRecord("dashboard", logging.ERROR, "", 0, message, (), None)
    RedactTokens().filter(record)
    return record.getMessage()


class Dashboard:
    def __init__(self) -> None:
        self.key = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.refresh_event = threading.Event()
        self.data = {"connected": False, "error": None, "updated": None}
        self.process: subprocess.Popen | None = None
        self.mode = "stopped"
        self.last_exit = None
        self.test_process: subprocess.Popen | None = None
        self.test_id = None
        self.profile = get_profile("ma_sber")
        self.paper = PaperSession(self.profile)

    def select_profile(self, identifier):
        profile = get_profile(identifier)
        with self.lock:
            if (
                self.paper.running
                or self.process
                and self.process.poll() is None
                or self.test_process
                and self.test_process.poll() is None
            ):
                raise RuntimeError(
                    "Остановите запуск и дождитесь исторического теста перед сменой профиля."
                )
            paper = PaperSession(profile)
            self.profile, self.paper = profile, paper
            self.data = {"connected": False, "error": None, "updated": None}
            self.refresh_event.set()

    def start_paper(self, expected_profile=None):
        with self.lock:
            if expected_profile is not None and expected_profile != self.profile.id:
                raise ValueError("Профиль изменился. Обновите панель.")
            if self.process and self.process.poll() is None or self.paper.running:
                raise RuntimeError("Сначала остановите текущий запуск.")
            self.paper.start(self.data)

    def start(self, execute: bool, expected_profile=None) -> None:
        settings = load_settings()
        if execute:
            ensure_auto_trading_allowed(settings)
        with self.lock:
            if expected_profile is not None and expected_profile != self.profile.id:
                raise ValueError("Профиль изменился. Обновите панель.")
            if self.paper.running or self.process and self.process.poll() is None:
                raise RuntimeError(
                    "Робот панели уже запущен. Сначала остановите процесс."
                )
            # A CLI bot uses the same OS lock. Check before starting; the child
            # acquires it itself, so even a race cannot create two trading loops.
            self.profile.state_path.parent.mkdir(parents=True, exist_ok=True)
            with state_lock(self.profile.state_path):
                pass
            args = [
                sys.executable,
                str(ROOT / "main.py"),
                "bot",
                "--loop",
                "--profile",
                self.profile.id,
            ]
            if execute:
                args.append("--execute")
            self.process = subprocess.Popen(
                args,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.mode = "execute" if execute else "observe"
            self.last_exit = None
        logger.info("Dashboard started bot PID=%s mode=%s", self.process.pid, self.mode)

    def stop(self) -> None:
        with self.lock:
            process = self.process
            if self.paper.running:
                self.paper.stop()
        if process and process.poll() is None:
            process.send_signal(signal.SIGINT if os.name != "nt" else signal.SIGTERM)
            logger.info(
                "Dashboard requested stop for PID=%s; position is not liquidated",
                process.pid,
            )
        # Do not block HTTP waiting for an in-flight broker RPC.

    def start_backtest(self, options: dict) -> str:
        from backtest import date_range
        from strategy import StrategyParams

        with self.lock:
            profile = self.profile
        if options.get("profile_id", profile.id) != profile.id:
            raise ValueError("Выбранный профиль изменился. Обновите панель.")
        days = int(options.get("days", 30))
        first, last = options.get("from_date") or None, options.get("to_date") or None
        date_range(days, first, last)
        params = StrategyParams(
            int(options.get("fast", profile.params.fast)),
            int(options.get("slow", profile.params.slow)),
            int(options.get("trend", profile.params.trend)),
            float(options.get("entry_edge_bps", profile.params.entry_edge_bps)),
            float(options.get("stop_pct", profile.params.stop_pct)),
            str(options.get("strategy_kind", profile.params.kind)),
        )
        fee, slip = float(options.get("fee_bps", profile.fee_bps)), float(
            options.get("slippage_bps", profile.slippage_bps)
        )
        if not all(math.isfinite(v) and 0 <= v <= 100 for v in [fee, slip]):
            raise ValueError("Комиссия и проскальзывание: 0..100 bps.")
        args = [
            sys.executable,
            str(ROOT / "main.py"),
            "backtest",
            "--profile",
            profile.id,
            "--days",
            str(days),
            "--fast",
            str(params.fast),
            "--slow",
            str(params.slow),
            "--trend",
            str(params.trend),
            "--entry-edge-bps",
            str(params.entry_edge_bps),
            "--stop-pct",
            str(params.stop_pct),
            "--commission-bps",
            str(fee),
            "--slippage-bps",
            str(slip),
            "--strategy-kind",
            params.kind,
        ]
        if first:
            args += ["--from-date", first, "--to-date", last]
        with self.lock:
            if profile.id != self.profile.id:
                raise ValueError(
                    "Профиль изменился. Повторите тест после обновления панели."
                )
            if self.test_process and self.test_process.poll() is None:
                raise RuntimeError("Исторический тест уже выполняется.")
            self.test_process = subprocess.Popen(
                args,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.test_id = uuid4().hex[:8]
        logger.info("Started read-only backtest job=%s: %s", self.test_id, args[3:])
        return self.test_id

    def status(self, lines: int = 70) -> dict:
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            if self.process and not running:
                self.last_exit = self.process.returncode
                self.mode = "stopped"
            profile = self.profile
            result = dict(self.data)
            result["panel_session"] = self.key[:8]
            result["profiles"] = [p.public() for p in profiles().values()]
            result["selected_profile"] = self.profile.public()
            result["paper"] = self.paper.snapshot(result.get("lot", 1))
            result["runner"] = {
                "running": running or self.paper.running,
                "mode": "paper" if self.paper.running else self.mode,
                "pid": self.process.pid if running else None,
                "exit_code": self.last_exit,
            }
        try:
            state_path = profile.state_path
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            result["bot_state"] = {
                key: state.get(key)
                for key in [
                    "held_lots",
                    "entry_price",
                    "entries",
                    "attempts",
                    "halted",
                    "pending",
                ]
            }
        except (OSError, ValueError) as exc:
            result["state_error"] = safe_message(exc)
        result["logs"] = read_logs(lines)
        from journal import latest

        try:
            result["journal"] = latest(100, path=database(state_path))
        except Exception as exc:
            result["journal"] = []
            result["journal_error"] = safe_message(exc)
        result["backtest_running"] = bool(
            self.test_process and self.test_process.poll() is None
        )
        result["backtest_id"] = self.test_id
        result["backtest_exit_code"] = (
            self.test_process.poll() if self.test_process else None
        )
        latest = ROOT / "reports" / "latest.json"
        if latest.exists():
            try:
                result["backtest"] = json.loads(latest.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                result["backtest_error"] = safe_message(exc)
        return result

    def refresh(self, broker: SandboxBroker) -> dict:
        with self.lock:
            profile = self.profile
        share = broker.find_share_by_ticker(profile.ticker)
        status = broker.trading_status(share)
        price, timestamp = broker.last_price(share)
        now = datetime.now(timezone.utc)
        candles = broker.client.market_data.get_candles(
            instrument_id=share.uid,
            from_=now - timedelta(days=10),
            to=now,
            interval=CandleInterval.CANDLE_INTERVAL_15_MIN,
        ).candles
        closed = sorted((c for c in candles if c.is_complete), key=lambda c: c.time)
        prices = [quotation_to_float(c.close) for c in closed]
        series = []
        for i, candle in enumerate(closed):
            series.append(
                {
                    "time": candle.time.isoformat(),
                    "close": prices[i],
                    "ma10": (
                        (
                            min(prices[i - profile.params.fast : i])
                            if profile.params.kind == "breakout"
                            else mean(prices[i - profile.params.fast + 1 : i + 1])
                        )
                        if i >= profile.params.fast
                        else None
                    ),
                    "ma30": (
                        (
                            max(prices[i - profile.params.slow : i])
                            if profile.params.kind == "breakout"
                            else mean(prices[i - profile.params.slow + 1 : i + 1])
                        )
                        if i >= profile.params.slow
                        else None
                    ),
                }
            )
        accounts = broker.client.sandbox.get_sandbox_accounts().accounts
        matches = [
            a
            for a in accounts
            if a.name == profile.account_name and a.status.name == "ACCOUNT_STATUS_OPEN"
        ]
        if len(matches) > 1:
            raise RuntimeError(
                "Несколько счетов trading-bot-auto; устраните неоднозначность."
            )
        account = None
        if matches:
            account_id = matches[0].id
            portfolio = broker.get_sandbox_portfolio(account_id)
            positions = broker.get_sandbox_positions(account_id)
            orders = broker.client.sandbox.get_sandbox_orders(
                account_id=account_id
            ).orders
            account = {
                "id": account_id,
                "equity": quotation_to_float(portfolio.total_amount_portfolio),
                "cash": sum(
                    quotation_to_float(m)
                    for m in positions.money
                    if m.currency == "rub"
                ),
                "blocked": sum(
                    quotation_to_float(m)
                    for m in positions.blocked
                    if m.currency == "rub"
                ),
                "positions": [
                    {"uid": p.instrument_uid, "shares": p.balance, "blocked": p.blocked}
                    for p in positions.securities
                ],
                "orders": [
                    {
                        "id": o.order_id,
                        "side": o.direction.name,
                        "status": o.execution_report_status.name,
                        "requested": o.lots_requested,
                        "executed": o.lots_executed,
                    }
                    for o in orders
                ],
            }
        return {
            "connected": True,
            "error": None,
            "updated": now.isoformat(),
            "ticker": share.ticker,
            "profile_id": profile.id,
            "currency": share.currency.lower(),
            "name": share.name,
            "lot": share.lot,
            "price": price,
            "quote_time": timestamp.isoformat(),
            "quote_age": (now - timestamp).total_seconds(),
            "market": {
                "status": status.trading_status.name,
                "market": status.market_order_available_flag,
                "limit": status.limit_order_available_flag,
                "api": status.api_trade_available_flag,
            },
            "signal": configured_signal(prices, profile.params),
            "series": series[
                -max(100, profile.params.slow + 1, profile.params.trend) :
            ],
            "account": account,
            "execution_allowed": broker.settings.enable_auto_trading
            and broker.settings.enable_sandbox_orders,
        }

    def worker(self) -> None:
        while not self.stop_event.is_set():
            self.refresh_event.clear()
            try:
                with SandboxBroker(load_settings()) as broker:
                    while not self.stop_event.is_set():
                        self.refresh_event.clear()
                        snapshot = self.refresh(broker)
                        with self.lock:
                            if snapshot["profile_id"] == self.profile.id:
                                self.data = snapshot
                                try:
                                    self.paper.update(snapshot)
                                except Exception:
                                    self.paper.running = False
                                    raise
                        self.refresh_event.wait(30)
            except Exception as exc:
                message = safe_message(exc)
                logger.error("Dashboard data refresh failed: %s", message)
                with self.lock:
                    self.data = {**self.data, "connected": False, "error": message}
                self.refresh_event.wait(30)


def read_logs(lines: int) -> list[str]:
    tail = deque(maxlen=lines)
    for path in [
        LOG_PATH.with_name(LOG_PATH.name + f".{i}") for i in range(5, 0, -1)
    ] + [LOG_PATH]:
        try:
            with path.open(encoding="utf-8") as file:
                tail.extend(line.rstrip("\n") for line in file)
        except FileNotFoundError:
            continue
    return list(tail)


def make_handler(app: Dashboard, port: int):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format, *args):
            # Polling does not flood the trading journal.
            pass

        def valid_host(self) -> bool:
            return self.headers.get("Host") in {
                f"127.0.0.1:{port}",
                f"localhost:{port}",
            }

        def reply(
            self, status: int, body: bytes, kind: str = "application/json"
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.valid_host():
                self.reply(403, b'{"error":"Invalid host"}')
            elif self.path == "/":
                self.reply(
                    200,
                    (ROOT / "web" / "index.html")
                    .read_text()
                    .replace("__CONTROL_KEY__", app.key)
                    .encode(),
                    "text/html",
                )
            elif urlsplit(self.path).path in {"/assets/panel.css", "/assets/panel.js"}:
                asset = Path(urlsplit(self.path).path).name
                kind = "text/css" if asset.endswith(".css") else "text/javascript"
                self.reply(200, (ROOT / "web" / asset).read_bytes(), kind)
            elif self.path == "/api/control-session":
                origin = self.headers.get("Origin")
                site = self.headers.get("Sec-Fetch-Site")
                if (
                    origin is not None
                    and origin
                    not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
                ) or site not in {None, "same-origin", "none"}:
                    self.reply(403, b'{"error":"Local session required"}')
                else:
                    self.reply(200, json.dumps({"key": app.key}).encode())
            elif urlsplit(self.path).path == "/api/status":
                try:
                    lines = int(
                        parse_qs(urlsplit(self.path).query).get("lines", ["70"])[0]
                    )
                    self.reply(
                        200,
                        json.dumps(
                            app.status(max(50, min(5000, lines))), ensure_ascii=False
                        ).encode(),
                    )
                except ValueError:
                    self.reply(400, b'{"error":"Invalid lines"}')
            elif self.path == "/api/logs/download":
                self.reply(200, "\n".join(read_logs(100000)).encode(), "text/plain")
            elif urlsplit(self.path).path == "/reports/latest":
                try:
                    latest = json.loads((ROOT / "reports" / "latest.json").read_text())
                    folder = latest["folder"]
                    if Path(folder).name != folder or not folder:
                        raise ValueError("Invalid report name")
                    self.reply(
                        200,
                        (ROOT / "reports" / folder / "report.html").read_bytes(),
                        "text/html",
                    )
                except (OSError, ValueError, KeyError):
                    self.reply(404, b'{"error":"No report yet"}')
            else:
                self.reply(404, b'{"error":"Not found"}')

        def do_POST(self):
            if (
                not self.valid_host()
                or self.headers.get("X-Control-Key") != app.key
                or self.headers.get("Origin")
                not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            ):
                self.reply(403, b'{"error":"Local control authorization required"}')
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 1024:
                    raise ValueError("Request too large")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Command body must be a JSON object")
                if self.path == "/api/start":
                    mode = body.get("mode")
                    if mode not in {"observe", "execute", "paper"}:
                        raise ValueError("Unknown mode")
                    if mode == "paper":
                        app.start_paper(body.get("profile_id"))
                    else:
                        (
                            app.start(mode == "execute", body["profile_id"])
                            if "profile_id" in body
                            else app.start(mode == "execute")
                        )
                elif self.path == "/api/profile/select":
                    app.select_profile(str(body.get("id", "")))
                elif self.path == "/api/profile/import":
                    profile = import_profile(body)
                    self.reply(
                        200,
                        json.dumps(
                            {"ok": True, "profile": profile.public()},
                            ensure_ascii=False,
                        ).encode(),
                    )
                    return
                elif self.path == "/api/backtest":
                    job = app.start_backtest(body)
                    self.reply(200, json.dumps({"ok": True, "job_id": job}).encode())
                    return
                elif self.path == "/api/stop":
                    app.stop()
                else:
                    self.reply(404, b'{"error":"Not found"}')
                    return
                self.reply(200, b'{"ok":true}')
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                self.reply(
                    400,
                    json.dumps(
                        {"error": safe_message(exc)}, ensure_ascii=False
                    ).encode(),
                )

    return Handler


def serve(port: int = 8765) -> None:
    configure_logging()
    app = Dashboard()
    bind_host = os.getenv("TRADING_PANEL_BIND", "127.0.0.1")
    if bind_host not in {"127.0.0.1", "0.0.0.0"}:
        raise ValueError("TRADING_PANEL_BIND must be 127.0.0.1 or 0.0.0.0")
    lock_path = ROOT / ".runtime" / ".dashboard.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with state_lock(lock_path):
        server = ThreadingHTTPServer((bind_host, port), make_handler(app, port))
        server.timeout = 1
        worker = threading.Thread(target=app.worker, daemon=True)
        worker.start()
        logger.info("Local Sandbox panel: http://127.0.0.1:%s", port)
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Stopping dashboard and its owned bot process")
        finally:
            app.stop_event.set()
            app.refresh_event.set()
            app.stop()
            if app.process:
                try:
                    app.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "Bot still finishing broker request, PID=%s; state preserved",
                        app.process.pid,
                    )
            server.server_close()
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    serve()
