"""Central configuration for AlphaGrid.

All thresholds, toggles, and credentials route through this module.
Locked values come from the architecture doc (2026-05-18).
Override anything via .env — never edit defaults in code.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Runtime(BaseSettings):
    paper_mode: bool = True
    log_level: str = "INFO"
    timezone: str = "Asia/Kolkata"
    alphagrid_db_url: str = f"sqlite:///{REPO_ROOT / 'alphagrid.db'}"
    redis_url: str = "redis://127.0.0.1:6379/0"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class AngelOne(BaseSettings):
    api_key: str = ""
    client_code: str = ""
    password: str = ""
    totp_secret: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="ANGEL_", extra="ignore")


class Binance(BaseSettings):
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_prefix="BINANCE_", extra="ignore")


class DataProviders(BaseSettings):
    glassnode_api_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "alphagrid/0.1"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class LLM(BaseSettings):
    anthropic_api_key: str = ""
    google_api_key: str = ""
    llm_provider: Literal["anthropic", "google"] = "anthropic"
    llm_model: str = "claude-haiku-4-5-20251001"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class Telegram(BaseSettings):
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    human_approval_required: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class RiskRules(BaseSettings):
    """Locked invariants — see project_alphagrid memory."""

    max_pct_per_trade: float = 0.02
    kill_switch_drawdown: float = 0.10
    kill_switch_window_days: int = 30
    kelly_fraction: float = 0.5
    kelly_lookback_trades: int = 50
    max_correlated_longs: int = 3
    correlation_threshold: float = 0.70
    indian_intraday_open: str = "09:15"
    indian_intraday_close: str = "15:25"


class AgentToggles(BaseSettings):
    enable_research_india: bool = True
    enable_research_crypto: bool = True
    enable_trading_momentum: bool = True
    enable_trading_sentiment: bool = True
    enable_trading_pairs: bool = True
    enable_trading_funding: bool = True
    enable_trading_trend: bool = True
    enable_trading_crypto_sent: bool = True


class StrategyParams(BaseSettings):
    """Locked strategy thresholds from the architecture doc."""

    sentiment_entry_threshold: float = 0.72
    sentiment_exit_threshold: float = 0.50
    sentiment_consecutive_windows: int = 3
    sentiment_max_holding_days: int = 5

    momentum_fast_ewma: int = 8
    momentum_slow_ewma: int = 32
    momentum_swing_confirm_days: int = 2

    pairs_zscore_entry: float = 2.0
    pairs_zscore_exit: float = 0.0
    pairs_cointegration_pvalue: float = 0.05
    pairs_universe: tuple[tuple[str, str], ...] = (
        ("HDFCBANK", "ICICIBANK"),
        ("RELIANCE", "ONGC"),
        ("INFY", "TCS"),
        ("BAJFINANCE", "BAJAJFINSV"),
    )

    funding_enter_bps: float = 0.01
    funding_exit_bps: float = 0.005
    funding_universe: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")

    trend_speeds: tuple[tuple[int, int], ...] = ((8, 32), (16, 64), (32, 128))
    trend_universe: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
    trend_target_portfolio_vol: float = 0.10
    trend_min_speeds_agreeing: int = 2

    crypto_sent_mvrv_bullish: float = 1.0
    crypto_sent_mvrv_bearish: float = 3.5
    crypto_sent_size_modifier: float = 0.20


class Settings:
    """Lazy-loaded composite. Access via get_settings()."""

    def __init__(self) -> None:
        self.runtime = Runtime()
        self.angel = AngelOne()
        self.binance = Binance()
        self.data = DataProviders()
        self.llm = LLM()
        self.telegram = Telegram()
        self.risk = RiskRules()
        self.agents = AgentToggles()
        self.strategy = StrategyParams()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
