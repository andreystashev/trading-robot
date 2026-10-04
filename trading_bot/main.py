"""Console commands for the sandbox learning prototype."""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from statistics import mean
from zoneinfo import ZoneInfo

# Use the macOS system resolver before gRPC is imported/initialized.
if sys.platform == "darwin":
    os.environ.setdefault("GRPC_DNS_RESOLVER", "native")

from t_tech.invest.exceptions import RequestError

from config import (
    ensure_auto_trading_allowed,
    ensure_sandbox_trading_allowed,
    load_settings,
)
from log_setup import LOG_PATH, configure_logging
from strategy import simple_signal
from t_invest_client import SandboxBroker, ShareInfo, quotation_to_float

DISPLAY_ZONE = ZoneInfo("Europe/Moscow")
logger = logging.getLogger(__name__)


def show_info(broker: SandboxBroker, share: ShareInfo) -> None:
    print(
        f"Ticker: {share.ticker}\nНазвание: {share.name}\nUID: {share.uid}\n"
        f"FIGI: {share.figi}\nLot size: {share.lot}\nCurrency: {share.currency}\n"
        f"Class code: {share.class_code}"
    )
    price, timestamp = broker.last_price(share)
    print(
        f"Instrument: {share.ticker}\nLast price: {price:.2f} {share.currency.upper()}"
    )
    print(f"Quote time: {timestamp.astimezone(DISPLAY_ZONE):%Y-%m-%d %H:%M:%S %Z}")
    logger.info("Last price: %.2f", price)


def show_portfolio(broker: SandboxBroker, account_id: str) -> None:
    portfolio = broker.get_sandbox_portfolio(account_id)
    positions = broker.get_sandbox_positions(account_id)
    print(f"Sandbox account: {account_id}")
    for cash in positions.money:
        print(f"Cash: {quotation_to_float(cash):.2f} {cash.currency.upper()}")
    for cash in positions.blocked:
        print(f"Blocked cash: {quotation_to_float(cash):.2f} {cash.currency.upper()}")
    total = portfolio.total_amount_portfolio
    print(f"Portfolio value: {quotation_to_float(total):.2f} {total.currency.upper()}")
    print("Portfolio positions:")
    for p in portfolio.positions:
        print(
            f"  {p.instrument_uid or p.figi}: quantity={quotation_to_float(p.quantity):g}, "
            f"lots={quotation_to_float(p.quantity_lots):g}, "
            f"price={quotation_to_float(p.current_price):.2f} {p.current_price.currency}"
        )
    print("Available securities (balance is shares, not lots):")
    for p in positions.securities:
        print(
            f"  {p.instrument_uid or p.figi}: balance={p.balance}, blocked={p.blocked}"
        )
    if not positions.securities:
        print("  No securities.")


def run_command(command: str, broker: SandboxBroker) -> None:
    if command == "sandbox":
        show_portfolio(broker, broker.get_or_create_sandbox_account())
        return
    if command == "order" and not broker.settings.enable_sandbox_orders:
        print(
            "Sandbox trading disabled. Set ENABLE_SANDBOX_ORDERS=true to test an order."
        )
        return
    share = broker.find_share_by_ticker(broker.settings.ticker)
    if command == "info":
        show_info(broker, share)
    elif command == "watch":
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            price, timestamp = broker.last_price(share)
            print(
                f"{datetime.now(DISPLAY_ZONE):%H:%M:%S} | {share.ticker} | "
                f"{price:.2f} {share.currency.upper()} | "
                f"quote {timestamp.astimezone(DISPLAY_ZONE):%Y-%m-%d %H:%M:%S}",
                flush=True,
            )
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(5, remaining))
    elif command in {"candles", "strategy"}:
        candles = sorted(broker.candles(share), key=lambda c: c.time)
        if not candles:
            print("No candles in the last 30 minutes; exchange may be closed.")
        if command == "candles":
            for c in candles[-5:]:
                print(
                    f"{c.time.astimezone(DISPLAY_ZONE):%Y-%m-%d %H:%M %Z}\n"
                    f"O: {quotation_to_float(c.open):g}\nH: {quotation_to_float(c.high):g}\n"
                    f"L: {quotation_to_float(c.low):g}\nC: {quotation_to_float(c.close):g}\n"
                    f"Volume: {c.volume}\nComplete: {c.is_complete}"
                )
        else:
            prices = [quotation_to_float(c.close) for c in candles if c.is_complete]
            print(f"Last prices (completed minute candle closes):\n{prices}")
            print(
                f"MA5: {mean(prices[-5:]):.4f}"
                if len(prices) >= 5
                else "MA5: insufficient data"
            )
            print(
                f"MA20: {mean(prices[-20:]):.4f}"
                if len(prices) >= 20
                else "MA20: insufficient data"
            )
            print(f"Signal: {simple_signal(prices)} (demonstration only)")
    elif command == "order":
        if share.ticker != "SBER":
            raise RuntimeError("Set TICKER=SBER for the one-lot sandbox test.")
        account_id = broker.get_or_create_sandbox_account()
        show_portfolio(broker, account_id)
        print(f"SBER lot size: {share.lot} shares\nBuying: 1 lot", flush=True)
        result = broker.buy_one_sber_lot(account_id, share)
        print(
            f"Order ID: {result.order_id}\nExecution status: {result.execution_report_status.name}\n"
            f"Requested lots: {result.lots_requested}\nExecuted lots: {result.lots_executed}"
        )
        for label, value in [
            ("Execution price (executed lots)", result.executed_order_price),
            ("Total amount", result.total_order_amount),
        ]:
            if value.currency:
                print(
                    f"{label}: {quotation_to_float(value):.2f} {value.currency.upper()}"
                )
        if result.message:
            print(f"Broker message: {result.message}")
        show_portfolio(broker, account_id)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="T-Invest Sandbox demo; real trading disabled"
    )
    parser.add_argument(
        "command",
        choices=[
            "info",
            "candles",
            "watch",
            "sandbox",
            "strategy",
            "order",
            "bot",
            "status",
            "smoke",
            "logs",
            "panel",
            "backtest",
        ],
        nargs="?",
        default="info",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Send strategy orders in Sandbox (bot only)",
    )
    parser.add_argument(
        "--loop", action="store_true", help="Run bot every 30 seconds until Ctrl+C"
    )
    parser.add_argument(
        "--days", type=int, default=30, help="Historical test period, 1..365 days"
    )
    parser.add_argument(
        "--commission-bps",
        type=float,
        default=5,
        help="Commission per side, 5 bps = 0.05%%",
    )
    parser.add_argument(
        "--slippage-bps", type=float, default=5, help="Adverse slippage per side"
    )
    parser.add_argument(
        "--replay", type=str, help="Repeat a saved backtest folder offline"
    )
    parser.add_argument("--from-date", help="First day YYYY-MM-DD, Moscow timezone")
    parser.add_argument("--to-date", help="Last day YYYY-MM-DD, inclusive")
    parser.add_argument("--fast", type=int, default=10)
    parser.add_argument("--slow", type=int, default=30)
    parser.add_argument("--trend", type=int, default=0)
    parser.add_argument(
        "--strategy-kind",
        choices=["ma", "breakout", "reversion", "pullback"],
        default="ma",
    )
    parser.add_argument("--entry-edge-bps", type=float, default=0)
    parser.add_argument("--stop-pct", type=float, default=1)
    parser.add_argument("--profile", help="Strategy profile ID from strategy_profiles/")
    parser.add_argument("--ticker", help="Instrument ticker for historical tests")
    args = parser.parse_args()
    if args.replay and args.command != "backtest":
        parser.error("--replay is only supported with backtest")
    if args.command != "bot" and (args.execute or args.loop):
        parser.error("--execute/--loop are only supported with bot")
    if args.command == "panel":
        from dashboard import serve

        serve()
        return 0
    configure_logging()
    # SDK error logs can include server metadata: report sanitized errors ourselves.
    logging.getLogger("t_tech").setLevel(logging.CRITICAL)
    if args.command == "logs":
        print(
            "".join(
                LOG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)[-50:]
            ),
            end="",
        )
        return 0
    settings = None
    try:
        if args.command == "backtest" and args.replay:
            from pathlib import Path
            from backtest import replay

            replay(Path(args.replay))
            return 0
        settings = load_settings()
        from decimal import Decimal
        from dataclasses import replace
        from strategy_catalog import get_profile

        profile = get_profile(args.profile) if args.profile else None
        if profile:
            settings = replace(
                settings,
                ticker=profile.ticker,
                sandbox_initial_balance=Decimal(str(profile.initial)),
            )
            # A profile supplies defaults; explicit test flags may override them.
            for attr, flag in [
                ("fast", "--fast"),
                ("slow", "--slow"),
                ("trend", "--trend"),
                ("entry_edge_bps", "--entry-edge-bps"),
                ("stop_pct", "--stop-pct"),
                ("strategy_kind", "--strategy-kind"),
            ]:
                if not any(v == flag or v.startswith(flag + "=") for v in sys.argv[1:]):
                    setattr(
                        args,
                        attr,
                        getattr(
                            profile.params, "kind" if attr == "strategy_kind" else attr
                        ),
                    )
            if not any(
                v == "--commission-bps" or v.startswith("--commission-bps=")
                for v in sys.argv[1:]
            ):
                args.commission_bps = profile.fee_bps
            if not any(
                v == "--slippage-bps" or v.startswith("--slippage-bps=")
                for v in sys.argv[1:]
            ):
                args.slippage_bps = profile.slippage_bps
        elif args.ticker:
            settings = replace(settings, ticker=args.ticker.strip().upper())
        if args.command == "bot" and args.execute:
            ensure_auto_trading_allowed(settings)
        if args.command == "smoke":
            ensure_sandbox_trading_allowed(settings)
        with SandboxBroker(settings) as broker:
            if args.command == "backtest":
                from backtest import run_backtest
                from strategy import StrategyParams

                run_backtest(
                    broker,
                    args.days,
                    args.commission_bps,
                    args.slippage_bps,
                    from_date=args.from_date,
                    to_date=args.to_date,
                    params=StrategyParams(
                        args.fast,
                        args.slow,
                        args.trend,
                        args.entry_edge_bps,
                        args.stop_pct,
                        args.strategy_kind,
                    ),
                    profile=profile.public() if profile else None,
                )
            elif args.command == "bot":
                from auto_trader import run_bot

                run_bot(
                    broker,
                    execute=args.execute,
                    loop=args.loop,
                    **(
                        {
                            "path": profile.state_path,
                            "params": profile.params,
                            "account_name": profile.account_name,
                        }
                        if profile
                        else {}
                    ),
                )
            elif args.command == "smoke":
                from sandbox_smoke import run_smoke

                run_smoke(broker)
            elif args.command == "status":
                share = broker.find_share_by_ticker(settings.ticker)
                status = broker.trading_status(share)
                logger.info(
                    "%s: status=%s; api=%s; market=%s; limit=%s",
                    share.ticker,
                    status.trading_status.name,
                    status.api_trade_available_flag,
                    status.market_order_available_flag,
                    status.limit_order_available_flag,
                )
                show_info(broker, share)
            else:
                run_command(args.command, broker)
        return 0
    except KeyboardInterrupt:
        logger.info("Stopped by Ctrl+C.")
        return 130
    except RequestError as exc:
        code = getattr(exc.code, "name", str(exc.code))
        reasons = {
            "UNAUTHENTICATED": "Invalid/expired token or wrong token type; use a sandbox token.",
            "PERMISSION_DENIED": "Token permissions do not allow this sandbox request.",
            "UNAVAILABLE": "Connection unavailable; check network and sandbox endpoint.",
            "DEADLINE_EXCEEDED": "Request timed out.",
            "RESOURCE_EXHAUSTED": "API request limit reached.",
        }
        detail = str(exc.details)
        api_reasons = {
            "30079": "Instrument unavailable for trading: wait for an available session.",
            "30098": "No trading currently taking place for this instrument.",
            "30034": "Insufficient sandbox cash.",
            "50005": "Order not found: inspect saved UUID and broker state before retrying.",
        }
        reason = api_reasons.get(detail, reasons.get(code, "Request failed."))
        if settings:
            detail = detail.replace(settings.token, "[REDACTED]")
        logger.error("Sandbox API error [%s]: %s %s", code, reason, detail)
        if args.command in {"order", "bot", "smoke"}:
            logger.error(
                "No order retry performed. If submission was attempted, inspect sandbox orders "
                "before another run: execution may have occurred despite the error."
            )
        return 1
    except (ValueError, RuntimeError, OSError) as exc:
        message = str(exc)
        if settings:
            message = message.replace(settings.token, "[REDACTED]")
        logger.error("%s", message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
