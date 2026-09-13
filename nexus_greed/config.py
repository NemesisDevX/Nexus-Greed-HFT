"""Knobs. Twist via NEXUS_<FIELD> env or CLI — never touch strategy.py."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConfig:
    # epsilon-greedy
    epsilon: float = 0.10           # probe-bid probability
    epsilon_decay: float = 0.995
    epsilon_floor: float = 0.02     # never go fully greedy

    # MA band
    ma_window: int = 12
    buy_z_threshold: float = -1.2   # sig below MA
    sell_z_threshold: float = 1.6   # sig above MA

    # scarcity / hoard
    scarcity_supply_pct: float = 0.18
    hoard_markup: float = 3.0
    hoard_max_fraction: float = 0.45

    # cornering
    obi_weight: float = 0.5
    cornering_cash_fraction: float = 0.35
    sweep_depth_fraction: float = 1.0  # lift the whole offer side
    squeeze_wall_levels: int = 3
    squeeze_wall_step: float = 0.10

    # spoof machine
    spoof_enabled: bool = True
    spoof_probability: float = 0.06
    spoof_duration: int = 6
    spoof_walls: int = 2
    spoof_depth_fraction: float = 0.60
    spoof_wall_price_mult: float = 1.6
    spoof_markup: float = 4.0
    spoof_frenzy_ticks: int = 8
    spoof_cooldown_ticks: int = 40
    spoof_min_saturation: float = 0.35  # don't bother spoofing a dead book
    dip_cash_fraction: float = 0.45

    report_ticks: int = 1000
    report_path: str = "backtest_report.md"

    starting_cash: float = 10_000.0
    max_position_per_resource: float = 500.0
    min_trade_size: float = 1.0
    slippage_bps: float = 25.0

    tick_seconds: float = 1.5
    max_ticks: int = 0               # 0 = run forever

    @classmethod
    def from_env(cls) -> "StrategyConfig":
        """NEXUS_<FIELD> env overrides."""
        overrides = {}
        for field in cls.__dataclass_fields__:
            raw = os.environ.get(f"NEXUS_{field.upper()}")
            if raw is None:
                continue
            kind = type(getattr(cls, field))
            if kind is bool:
                overrides[field] = raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                overrides[field] = kind(raw)  # type: ignore[arg-type]
        return cls(**overrides)


# order matters for the log
TRADED_RESOURCES = ("cpu_cores", "gpu_slices", "ram_pages", "bandwidth_mbps")
