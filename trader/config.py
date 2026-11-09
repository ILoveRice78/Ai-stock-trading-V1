"""Load and validate config.toml.

Standard library only: the trade guard hook imports this module and must work
without third-party packages.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class GeneralConfig:
    mode: str = "paper"
    trading_enabled: bool = True
    timezone: str = "America/New_York"


@dataclass(frozen=True)
class WatchlistConfig:
    symbols: tuple[str, ...] = ("SPY",)
    benchmark: str = "SPY"


@dataclass(frozen=True)
class DataConfig:
    source: str = "yfinance"
    files_dir: str = "data/bars"
    history_days: int = 600


@dataclass(frozen=True)
class PortfolioConfig:
    core_pct: float = 80.0
    trend_pct: float = 15.0
    rebalance_band_pct: float = 25.0
    reentry_wait_days: int = 10


@dataclass(frozen=True)
class StrategyConfig:
    sma_fast: int = 50
    sma_slow: int = 200
    atr_period: int = 14
    trailing_stop_atr: float = 3.0
    volume_lookback: int = 20


@dataclass(frozen=True)
class RiskConfig:
    starting_paper_cash: float = 10_000.0
    stop_loss_pct: float = 10.0
    min_cash_reserve_pct: float = 5.0
    daily_loss_limit_pct: float = 3.0
    max_drawdown_pct: float = 15.0
    min_price: float = 5.0
    min_avg_dollar_volume: float = 20_000_000.0
    fractional_shares: bool = True


@dataclass(frozen=True)
class OrdersConfig:
    order_type: str = "limit"
    limit_buffer_pct: float = 0.5
    ticket_ttl_minutes: int = 120
    slippage_bps: float = 5.0
    account_max_age_minutes: int = 30


@dataclass(frozen=True)
class ReviewConfig:
    avoid_earnings_within_days: int = 5


@dataclass(frozen=True)
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    watchlist: WatchlistConfig = field(default_factory=WatchlistConfig)
    data: DataConfig = field(default_factory=DataConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    orders: OrdersConfig = field(default_factory=OrdersConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    path: Path | None = None

    @property
    def is_live(self) -> bool:
        return self.general.mode == "live"


_SECTIONS = {
    "general": GeneralConfig,
    "watchlist": WatchlistConfig,
    "data": DataConfig,
    "portfolio": PortfolioConfig,
    "strategy": StrategyConfig,
    "risk": RiskConfig,
    "orders": OrdersConfig,
    "review": ReviewConfig,
}


def _build_section(cls: type, raw: dict[str, Any], section: str) -> Any:
    known = {f.name: f for f in fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"[{section}] unknown setting(s): {', '.join(sorted(unknown))}")
    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        default = getattr(cls(), name)
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"[{section}] {name} must be true or false")
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"[{section}] {name} must be a number")
            value = float(value)
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"[{section}] {name} must be a whole number")
        elif isinstance(default, tuple):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"[{section}] {name} must be a list of strings")
            value = tuple(v.strip().upper() for v in value)
        elif isinstance(default, str):
            if not isinstance(value, str):
                raise ConfigError(f"[{section}] {name} must be text")
        kwargs[name] = value
    return cls(**kwargs)


def _validate(cfg: Config) -> None:
    if cfg.general.mode not in ("paper", "live"):
        raise ConfigError('[general] mode must be "paper" or "live"')
    if cfg.data.source not in ("yfinance", "files"):
        raise ConfigError('[data] source must be "yfinance" or "files"')
    if cfg.orders.order_type not in ("limit", "market"):
        raise ConfigError('[orders] order_type must be "limit" or "market"')
    if not cfg.watchlist.symbols:
        raise ConfigError("[watchlist] symbols must not be empty")
    r = cfg.risk
    for name in ("daily_loss_limit_pct", "max_drawdown_pct"):
        if not 0 < getattr(r, name) <= 100:
            raise ConfigError(f"[risk] {name} must be between 0 and 100")
    if not 0 <= r.min_cash_reserve_pct < 100:
        raise ConfigError("[risk] min_cash_reserve_pct must be between 0 and 100")
    if not 0 <= r.stop_loss_pct < 50:
        raise ConfigError("[risk] stop_loss_pct must be between 0 (off) and 50")
    pf = cfg.portfolio
    if pf.core_pct < 0 or pf.trend_pct < 0 or pf.core_pct + pf.trend_pct <= 0:
        raise ConfigError("[portfolio] core_pct and trend_pct must be 0 or more, and not both 0")
    if pf.core_pct + pf.trend_pct + r.min_cash_reserve_pct > 100 + 1e-9:
        raise ConfigError(
            "[portfolio] core_pct + trend_pct + [risk] min_cash_reserve_pct must not exceed 100"
        )
    if not 0 < pf.rebalance_band_pct <= 100:
        raise ConfigError("[portfolio] rebalance_band_pct must be between 0 and 100")
    if pf.reentry_wait_days < 0:
        raise ConfigError("[portfolio] reentry_wait_days must be 0 or more")
    s = cfg.strategy
    if s.sma_fast >= s.sma_slow:
        raise ConfigError("[strategy] sma_fast must be smaller than sma_slow")


def config_path() -> Path:
    return Path(os.environ.get("TRADER_CONFIG", PROJECT_ROOT / "config.toml"))


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else config_path()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    unknown = set(raw) - set(_SECTIONS)
    if unknown:
        raise ConfigError(f"unknown config section(s): {', '.join(sorted(unknown))}")
    sections = {
        name: _build_section(cls, raw.get(name, {}), name) for name, cls in _SECTIONS.items()
    }
    cfg = Config(**sections, path=path)
    _validate(cfg)
    return cfg
