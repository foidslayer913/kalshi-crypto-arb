import asyncio

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from config import Settings
from execution.demo_trader import BookQuote, DemoTrader, KillSwitch, KillSwitchTripped


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


def test_kill_switch_not_tripped_below_limit():
    kill_switch = KillSwitch(max_daily_loss=100.0)
    kill_switch.record_pnl(-40.0)
    assert kill_switch.tripped is False
    kill_switch.check()  # should not raise


def test_kill_switch_trips_at_limit():
    kill_switch = KillSwitch(max_daily_loss=100.0)
    kill_switch.record_pnl(-60.0)
    kill_switch.record_pnl(-45.0)
    assert kill_switch.tripped is True
    with pytest.raises(KillSwitchTripped):
        kill_switch.check()


def test_kill_switch_ignores_gains():
    kill_switch = KillSwitch(max_daily_loss=10.0)
    kill_switch.record_pnl(50.0)
    assert kill_switch.realized_loss == 0.0
    assert kill_switch.tripped is False


def test_kill_switch_reset():
    kill_switch = KillSwitch(max_daily_loss=10.0)
    kill_switch.record_pnl(-20.0)
    assert kill_switch.tripped is True
    kill_switch.reset()
    assert kill_switch.tripped is False
    kill_switch.check()  # should not raise


def _quote(price: float, depth: int = 5) -> BookQuote:
    return BookQuote(ticker="KXBTC-24JUL2915", side="yes", ask_price=price, ask_depth=depth)


def test_simulate_fill_no_phantom_when_book_unchanged(demo_trader):
    assert demo_trader.simulate_fill(_quote(0.95), _quote(0.95)) is False


def test_simulate_fill_detects_price_moved_up(demo_trader):
    assert demo_trader.simulate_fill(_quote(0.95), _quote(0.97)) is True


def test_simulate_fill_detects_depth_dried_up(demo_trader):
    assert demo_trader.simulate_fill(_quote(0.95, depth=5), _quote(0.95, depth=0)) is True


def test_simulate_fill_rejects_mismatched_quotes(demo_trader):
    mismatched = BookQuote(ticker="OTHER-TICKER", side="yes", ask_price=0.95, ask_depth=5)
    with pytest.raises(ValueError):
        demo_trader.simulate_fill(_quote(0.95), mismatched)


def test_place_order_dry_run_returns_result_without_network(demo_trader):
    result = asyncio.run(
        demo_trader.place_order(
            ticker="KXBTC-24JUL2915", side="yes", count=1, price=0.95,
            decision_quote=_quote(0.95), current_quote=_quote(0.97),
        )
    )
    assert result.dry_run is True
    assert result.phantom_fill is True


def test_place_order_blocked_when_kill_switch_tripped(tmp_path):
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
    kill_switch = KillSwitch(max_daily_loss=10.0)
    kill_switch.record_pnl(-20.0)
    trader = DemoTrader(settings, kill_switch=kill_switch)
    with pytest.raises(KillSwitchTripped):
        asyncio.run(trader.place_order(ticker="KXBTC-24JUL2915", side="yes", count=1, price=0.95))


def test_demo_trader_rejects_non_demo_base_url(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValueError):
        Settings(
            KALSHI_API_KEY_ID="test-key",
            KALSHI_PRIVATE_KEY_PATH=str(key_path),
            KALSHI_BASE_URL="https://api.kalshi.co/trade-api/v2",
        )
