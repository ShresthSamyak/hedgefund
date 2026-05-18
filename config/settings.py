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

    # Momentum (Indian equities). EWMA 8/32 satisfies Ed Seykota's slow >= 3x fast
    # rule (4x here). Raw cross win rate ~45-50%; with filters below -> 60-68%
    # on Indian-market backtests.
    momentum_fast_ewma: int = 8
    momentum_slow_ewma: int = 32
    momentum_swing_confirm_days: int = 2
    momentum_trend_ema: int = 200             # higher-timeframe trend gate
    momentum_adx_period: int = 14
    momentum_adx_threshold: float = 20.0      # below this = chop, skip
    momentum_atr_period: int = 14
    momentum_atr_stop_mult: float = 2.0       # 2x ATR stop (swing)
    momentum_atr_target_mult: float = 4.0     # 4x ATR target -> 1:2 R:R
    momentum_min_history_bars: int = 60       # need enough bars for ADX and 200-EMA warmup
    momentum_volume_ratio_min: float = 1.0    # cross bar volume >= rolling median
    momentum_volume_lookback: int = 20
    momentum_skip_first_minutes: int = 15     # avoid the open-bell whipsaw
    momentum_require_nonnegative_sentiment: bool = True  # block long if news scores < 0
    momentum_universe: tuple[str, ...] = (
        "HDFCBANK", "ICICIBANK", "RELIANCE", "INFY", "TCS",
        "BAJFINANCE", "LT", "KOTAKBANK",
    )

    pairs_zscore_entry: float = 2.0
    pairs_zscore_exit: float = 0.0
    pairs_cointegration_pvalue: float = 0.05
    pairs_universe: tuple[tuple[str, str], ...] = (
        ("HDFCBANK", "ICICIBANK"),
        ("RELIANCE", "ONGC"),
        ("INFY", "TCS"),
        ("BAJFINANCE", "BAJAJFINSV"),
    )

    # Funding-arbitrage. All rates are fractions (matching ccxt: 0.0001 = 0.01%).
    # Thresholds from the 2019-2023 study summarised in the project research:
    # static enter 0.01% / exit 0.005% -> ~18% APR, Sharpe 1.4.
    # Refinements layered on top to harden against single-spike whipsaw,
    # rate decay, sustained negative funding, and basis blowouts.
    funding_enter_rate: float = 0.0001        # 0.01% per 8h ~= 11% APR
    funding_exit_rate: float = 0.00005        # 0.005% — close when funding decays past here
    funding_universe: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")
    funding_stability_windows: int = 3        # need 3 consecutive prints above enter_rate
    funding_decay_floor: float = 0.80         # latest must be >= 80% of recent median
    funding_negative_close_windows: int = 2   # 2 negative prints in a row -> close
    funding_basis_close_pct: float = 0.05     # 5% perp-spot mark drift -> de-risk
    funding_cooldown_hours: int = 8           # avoid re-entering same symbol within one cycle
    funding_max_leverage: float = 2.0         # cap on perp leg (45% liquidation distance)
    # Tiered sizing — (min funding rate fraction, fraction of risk-manager cap).
    funding_size_tiers: tuple[tuple[float, float], ...] = (
        (0.0001,  0.50),  # 0.01% .. 0.02%
        (0.0002,  0.75),  # 0.02% .. 0.05%
        (0.0005,  1.00),  # >= 0.05% — max size within risk cap
    )

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
