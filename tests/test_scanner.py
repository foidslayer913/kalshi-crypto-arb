import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from clock import VirtualClock
from config import Settings
from execution.demo_trader import DemoTrader, KillSwitch
from ingestion.crypto_feed import CryptoIndexFeed, PriceTick
from ingestion.kalshi_rest import MarketInfo
from ingestion.order_book import OrderBookStore
from strategy.math_engine import relative_cap
from strategy.scanner import ScannerConfig, SettlementArbScanner, resolve_crypto_symbol
from telemetry.logger import TelemetryLogger


def test_resolve_crypto_symbol_matches_prefix():
    mapping = {"KXBTC": "BTC-USD", "KXETH": "ETH-USD"}
    assert resolve_crypto_symbol("KXBTC-24JUL2915-T50000", mapping) == "BTC-USD"
    assert resolve_crypto_symbol("KXETH-24JUL2915-T3000", mapping) == "ETH-USD"
    assert resolve_crypto_symbol("UNKNOWN-TICKER", mapping) is None


@pytest.fixture
def demo_trader(tmp_path) -> DemoTrader:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    settings = Settings(KALSHI_API_KEY_ID="test-key", KALSHI_PRIVATE_KEY_PATH=str(key_path))
    return DemoTrader(settings, kill_switch=KillSwitch(max_daily_loss=100.0))


def _snapshot(yes=(), no=()):
    # Live wire shape: dollar-string prices, fixed-point-string sizes.
    def levels(pairs):
        return [[f"{price / 100:.4f}", f"{qty:.2f}"] for price, qty in pairs]

    return {
        "type": "orderbook_snapshot",
        "msg": {"market_ticker": "KXBTC-TEST", "yes_dollars_fp": levels(yes), "no_dollars_fp": levels(no)},
    }


def _make_scanner(demo_trader, tmp_path, strike_type="greater", strike_price=50.0):
    market = MarketInfo(
        ticker="KXBTC-TEST", strike_type=strike_type, strike_price=strike_price,
        close_time=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    crypto_feed = CryptoIndexFeed(["BTC-USD"])
    order_book = OrderBookStore()
    telemetry = TelemetryLogger(tmp_path / "telemetry.csv")
    scanner = SettlementArbScanner(
        market, "BTC-USD", crypto_feed=crypto_feed, order_book=order_book,
        trader=demo_trader, telemetry=telemetry, config=ScannerConfig(),
    )
    return scanner, crypto_feed, order_book, telemetry


def test_latest_price_returns_none_when_stale(demo_trader, tmp_path):
    scanner, crypto_feed, _, _ = _make_scanner(demo_trader, tmp_path)
    crypto_feed.buffer("BTC-USD").append(
        PriceTick(symbol="BTC-USD", price=100.0, timestamp=time.time() - 10)
    )
    assert scanner._latest_price(time.time()) is None


def test_latest_price_returns_fresh_tick(demo_trader, tmp_path):
    scanner, crypto_feed, _, _ = _make_scanner(demo_trader, tmp_path)
    now = time.time()
    crypto_feed.buffer("BTC-USD").append(PriceTick(symbol="BTC-USD", price=123.0, timestamp=now))
    assert scanner._latest_price(now) == 123.0


def test_maybe_trade_fires_when_guaranteed_and_yield_sufficient(demo_trader, tmp_path):
    scanner, _, order_book, telemetry = _make_scanner(demo_trader, tmp_path, strike_price=50.0)
    for _ in range(60):
        scanner._window.record_tick(100.0)  # floor average = 100 >> strike 50
    # no bid at 5 cents -> yes ask = 100 - 5 = 95 cents; fee $0.01 -> net yield 0.04 >= 0.01
    order_book.apply(
        _snapshot(no=[[5, 50]])
    )
    asyncio.run(scanner._maybe_trade())
    assert scanner._executed is True
    assert telemetry.summary()["count"] == 1


def test_maybe_trade_does_nothing_when_not_guaranteed(demo_trader, tmp_path):
    scanner, _, order_book, telemetry = _make_scanner(demo_trader, tmp_path, strike_price=1000.0)
    for _ in range(30):
        scanner._window.record_tick(100.0)  # floor average well below strike 1000
    order_book.apply(
        _snapshot(no=[[5, 50]])
    )
    asyncio.run(scanner._maybe_trade())
    assert scanner._executed is False
    assert telemetry.summary()["count"] == 0


def test_maybe_trade_skips_when_no_ask_available(demo_trader, tmp_path):
    scanner, _, _, telemetry = _make_scanner(demo_trader, tmp_path, strike_price=50.0)
    for _ in range(60):
        scanner._window.record_tick(100.0)
    asyncio.run(scanner._maybe_trade())
    assert scanner._executed is False
    assert telemetry.summary()["count"] == 0


def test_maybe_trade_skips_when_yield_too_low(demo_trader, tmp_path):
    scanner, _, order_book, telemetry = _make_scanner(demo_trader, tmp_path, strike_price=50.0)
    for _ in range(60):
        scanner._window.record_tick(100.0)
    # no bid at 1 cent -> yes ask = 0.99; fee $0.01 -> net yield 0.00 < default 0.01 threshold
    order_book.apply(
        _snapshot(no=[[1, 50]])
    )
    asyncio.run(scanner._maybe_trade())
    assert scanner._executed is False
    assert telemetry.summary()["count"] == 0


def test_maybe_trade_never_fires_twice(demo_trader, tmp_path):
    scanner, _, order_book, telemetry = _make_scanner(demo_trader, tmp_path, strike_price=50.0)
    for _ in range(60):
        scanner._window.record_tick(100.0)
    order_book.apply(
        _snapshot(no=[[5, 50]])
    )
    asyncio.run(scanner._maybe_trade())
    asyncio.run(scanner._maybe_trade())
    assert telemetry.summary()["count"] == 1


def test_less_strike_type_trades_on_yes_side(demo_trader, tmp_path):
    # A "less" market's YES pays out below the cap, so a ceiling under the strike means YES wins.
    scanner, _, order_book, telemetry = _make_scanner(
        demo_trader, tmp_path, strike_type="less", strike_price=200.0
    )
    for _ in range(60):
        scanner._window.record_tick(0.0)  # ceiling average = 0 << strike 200
    # no bid at 5 cents -> yes ask = 100 - 5 = 95 cents; fee $0.01 -> net yield 0.04 >= 0.01
    order_book.apply(
        _snapshot(no=[[5, 50]])
    )
    asyncio.run(scanner._maybe_trade())
    assert scanner.executed is True
    assert telemetry.summary()["count"] == 1


def test_greater_market_trades_no_side_when_guaranteed_below(demo_trader, tmp_path):
    # The symmetric opportunity: a ceiling below the strike decides a "greater" market against
    # YES, so NO is the guaranteed side.
    scanner, _, order_book, telemetry = _make_scanner(
        demo_trader, tmp_path, strike_type="greater", strike_price=200.0
    )
    scanner._window.cap_policy = relative_cap(0.01)
    for _ in range(10):
        scanner._window.record_tick(100.0)  # ceiling ~101 << strike 200
    # yes bid at 5 cents -> no ask = 95 cents
    order_book.apply(
        _snapshot(yes=[[5, 50]])
    )
    asyncio.run(scanner._maybe_trade())
    assert scanner.executed is True
    assert telemetry.summary()["count"] == 1


def test_scanner_run_uses_injected_clock(demo_trader, tmp_path):
    # A full 60-second window must replay without spending 60 real seconds.
    clock = VirtualClock(start=datetime.now(timezone.utc).timestamp())
    market = MarketInfo(
        ticker="KXBTC-TEST", strike_type="greater", strike_price=50.0,
        close_time=datetime.now(timezone.utc) + timedelta(seconds=3600),
    )
    crypto_feed = CryptoIndexFeed(["BTC-USD"])
    scanner = SettlementArbScanner(
        market, "BTC-USD", crypto_feed=crypto_feed, order_book=OrderBookStore(),
        trader=demo_trader, telemetry=TelemetryLogger(tmp_path / "telemetry.csv"),
        clock=clock,
    )

    started = time.monotonic()
    asyncio.run(scanner.run())
    real_elapsed = time.monotonic() - started

    assert real_elapsed < 2.0  # an hour of waiting plus a 60s window, simulated
    assert scanner.window.ticks_recorded == 60
