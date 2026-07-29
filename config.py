from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables / a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    kalshi_api_key_id: str = Field(alias="KALSHI_API_KEY_ID")
    kalshi_private_key_path: Path = Field(alias="KALSHI_PRIVATE_KEY_PATH")
    kalshi_base_url: str = Field(
        default="https://external-api.demo.kalshi.co/trade-api/v2",
        alias="KALSHI_BASE_URL",
    )
    kalshi_ws_url: str = Field(
        default="wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
        alias="KALSHI_WS_URL",
    )
    market_tickers_raw: str = Field(default="", alias="MARKET_TICKERS")
    crypto_feed_symbols_raw: str = Field(default="BTC-USD,ETH-USD", alias="CRYPTO_FEED_SYMBOLS")
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    max_daily_loss: float = Field(default=100.0, alias="MAX_DAILY_LOSS")

    @field_validator("kalshi_base_url", "kalshi_ws_url")
    @classmethod
    def _must_be_demo_environment(cls, value: str) -> str:
        # Hard safety guardrail from SPEC.md: this bot may never target Kalshi's live API.
        value = value.rstrip("/")
        if "demo" not in value:
            raise ValueError(
                f"Refusing to configure non-demo Kalshi endpoint: {value!r}. "
                "This bot is restricted to the Kalshi Demo API."
            )
        return value

    @property
    def market_tickers(self) -> list[str]:
        return [ticker.strip() for ticker in self.market_tickers_raw.split(",") if ticker.strip()]

    @property
    def crypto_feed_symbols(self) -> list[str]:
        return [symbol.strip() for symbol in self.crypto_feed_symbols_raw.split(",") if symbol.strip()]

    @property
    def private_key_pem(self) -> bytes:
        return self.kalshi_private_key_path.read_bytes()


def load_settings() -> Settings:
    return Settings()
