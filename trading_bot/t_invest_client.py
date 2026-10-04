"""Small wrapper around the official synchronous sandbox API."""

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from t_tech.invest import (
    AccountStatus,
    CandleInterval,
    Client,
    HistoricCandle,
    InstrumentIdType,
    InstrumentType,
    MoneyValue,
    OrderDirection,
    OrderType,
    PortfolioResponse,
    PositionsResponse,
    PostOrderResponse,
    Quotation,
    RealExchange,
)

from t_tech.invest.services import Services
from t_tech.invest import _error_hub

from config import Settings, ensure_sandbox_trading_allowed
from rpc_limits import DeadlineInterceptor

SANDBOX_ENDPOINT = "sandbox-invest-public-api.tbank.ru:443"
ACCOUNT_NAME = "trading-bot-test"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShareInfo:
    ticker: str
    name: str
    uid: str
    figi: str
    lot: int
    currency: str
    class_code: str


def quotation_to_float(value: Quotation | MoneyValue) -> float:
    return value.units + value.nano / 1_000_000_000


class SandboxBroker:
    def __init__(self, settings: Settings) -> None:
        if not settings.sandbox:
            raise RuntimeError("Real trading is disabled in this demo.")
        self.settings = settings
        self._context = Client(
            settings.token,
            target=SANDBOX_ENDPOINT,
            app_name="trading-bot-test",
            interceptors=[DeadlineInterceptor()],
        )
        self._client: Services | None = None
        self._order_attempted = False

    def __enter__(self) -> "SandboxBroker":
        # SDK 1.51.0 initializes Sentry on entry. An empty DSN disables its
        # transport before any requests. Keep this shim covered when upgrading.
        _error_hub.ERROR_HUB_DSN = ""
        self._client = self._context.__enter__()
        # Opening gRPC channel is lazy; verify authentication with a read request.
        try:
            self.client.sandbox.get_sandbox_accounts()
        except BaseException:
            self._context.__exit__(*sys.exc_info())
            self._client = None
            raise
        logger.info("Connected to T-Invest Sandbox")
        return self

    def __exit__(self, *args: object) -> None:
        self._context.__exit__(*args)
        self._client = None

    @property
    def client(self) -> Services:
        if self._client is None:
            raise RuntimeError("Use SandboxBroker inside a with block.")
        return self._client

    def find_share_by_ticker(self, ticker: str) -> ShareInfo:
        results = self.client.instruments.find_instrument(
            query=ticker,
            instrument_kind=InstrumentType.INSTRUMENT_TYPE_SHARE,
            api_trade_available_flag=True,
        ).instruments
        candidates = []
        for item in results:
            if item.ticker.upper() != ticker.upper():
                continue
            share = self.client.instruments.share_by(
                id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
                id=item.uid,
            ).instrument
            # TQBR = regular MOEX shares board; exclude OTC and foreign listings.
            # API availability describes instrument support, not current session.
            if (
                share.class_code == "TQBR"
                and share.real_exchange == RealExchange.REAL_EXCHANGE_MOEX
                and share.api_trade_available_flag
                and not share.otc_flag
            ):
                candidates.append(share)
        if len(candidates) != 1:
            raise RuntimeError(
                f"Expected one tradable MOEX TQBR share for {ticker}; "
                f"found {len(candidates)}. Check TICKER."
            )
        s = candidates[0]
        logger.info("Found %s", s.ticker)
        return ShareInfo(
            s.ticker, s.name, s.uid, s.figi, s.lot, s.currency, s.class_code
        )

    def trading_status(self, share: ShareInfo):
        statuses = self.client.market_data.get_trading_statuses(
            instrument_ids=[share.uid],
        ).trading_statuses
        if len(statuses) != 1:
            raise RuntimeError("No unique trading status returned for SBER.")
        return statuses[0]

    def last_price(self, share: ShareInfo) -> tuple[float, datetime]:
        prices = self.client.market_data.get_last_prices(
            instrument_id=[share.uid],
        ).last_prices
        if not prices or quotation_to_float(prices[0].price) <= 0:
            raise RuntimeError(f"No quotation available for {share.ticker}.")
        p = prices[0]
        return quotation_to_float(p.price), p.time

    def candles(self, share: ShareInfo) -> list[HistoricCandle]:
        now = datetime.now(timezone.utc)
        return self.client.market_data.get_candles(
            instrument_id=share.uid,
            from_=now - timedelta(minutes=30),
            to=now,
            interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
        ).candles

    def get_or_create_sandbox_account(self, name: str = ACCOUNT_NAME) -> str:
        accounts = self.client.sandbox.get_sandbox_accounts().accounts
        matching = [
            a
            for a in accounts
            if a.name == name and a.status == AccountStatus.ACCOUNT_STATUS_OPEN
        ]
        if len(matching) > 1:
            raise RuntimeError(
                f"Multiple {name} accounts found; "
                "resolve duplicates before continuing."
            )
        if matching:
            return matching[0].id
        account_id = self.client.sandbox.open_sandbox_account(
            name=name,
        ).account_id
        logger.info("Created sandbox account: %s", account_id)
        # Deposit only at creation. A failed deposit is surfaced, never retried.
        self.sandbox_pay_in(account_id, self.settings.sandbox_initial_balance)
        return account_id

    def sandbox_pay_in(self, account_id: str, amount: Decimal) -> None:
        if not self.settings.sandbox:
            raise RuntimeError("Real trading is disabled in this demo.")
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Sandbox deposit must be positive and finite.")
        nanos = amount * 1_000_000_000
        if nanos != nanos.to_integral_value():
            raise ValueError("Sandbox deposit supports at most 9 decimals.")
        units, nano = divmod(int(nanos), 1_000_000_000)
        self.client.sandbox.sandbox_pay_in(
            account_id=account_id,
            amount=MoneyValue(currency="rub", units=units, nano=nano),
        )

    def get_sandbox_portfolio(self, account_id: str) -> PortfolioResponse:
        return self.client.sandbox.get_sandbox_portfolio(account_id=account_id)

    def get_sandbox_positions(self, account_id: str) -> PositionsResponse:
        return self.client.sandbox.get_sandbox_positions(account_id=account_id)

    def buy_one_sber_lot(self, account_id: str, share: ShareInfo) -> PostOrderResponse:
        ensure_sandbox_trading_allowed(self.settings)
        if share.ticker != "SBER" or share.class_code != "TQBR":
            raise RuntimeError("This demo only permits buying SBER on TQBR.")
        if self._order_attempted:
            raise RuntimeError("Only one order attempt is allowed per run.")
        status = self.trading_status(share)
        if not (status.api_trade_available_flag and status.market_order_available_flag):
            raise RuntimeError(
                "Market orders unavailable: exchange closed or "
                "instrument temporarily restricted."
            )
        order_id = str(uuid4())
        self._order_attempted = True
        logger.info("Submitting ONE sandbox BUY, 1 lot; request UUID: %s", order_id)
        # No retries: a transport error can leave execution outcome uncertain.
        return self.client.sandbox.post_sandbox_order(
            account_id=account_id,
            instrument_id=share.uid,
            quantity=1,
            direction=OrderDirection.ORDER_DIRECTION_BUY,
            order_type=OrderType.ORDER_TYPE_MARKET,
            order_id=order_id,
        )
