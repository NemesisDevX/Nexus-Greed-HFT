"""Tunable strategy parameters for Nexus-Greed.

Every constant here is a knob the quant desk can twist without touching the
strategy code. Values are calibrated for a volatile, low-liquidity A2A
resource market where compute (CPU-cores, GPU-slices, RAM-pages, bandwidth)
is the traded good.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConfig:
    # --- Epsilon-Greedy exploration ---------------------------------------
    epsilon: float = 0.10           # probability of a random "probe" bid
    epsilon_decay: float = 0.995    # multiplicative decay per tick
    epsilon_floor: float = 0.02     # never stop probing entirely

    # --- Moving-average arbitrage band ------------------------------------
    ma_window: int = 12             # ticks used for the rolling fair-value
    buy_z_threshold: float = -1.2   # buy when price is 1.2 sigma *below* MA
    sell_z_threshold: float = 1.6   # sell when price is 1.6 sigma *above* MA

    # --- Hoarding / scarcity regime --------------------------------------
    scarcity_supply_pct: float = 0.18  # supply below 18% of capacity => scarce
    hoard_markup: float = 3.0          # 300% markup when supply is scarce
    hoard_max_fraction: float = 0.45   # never dump more than 45% of inventory

    # --- Predatory liquidity / market cornering ---------------------------
    obi_weight: float = 0.5            # OBI tilt applied to the z-score trigger
    cornering_cash_fraction: float = 0.35  # max share of cash per sweep
    sweep_depth_fraction: float = 1.0  # fraction of visible ask depth to lift
    squeeze_wall_levels: int = 3       # laddered limit-sell walls post-sweep
    squeeze_wall_step: float = 0.10    # each wall priced +10% above the prior

    # --- Market manipulation (spoofing & layering) -------------------------
    spoof_enabled: bool = True         # master switch for the spoof machine
    spoof_probability: float = 0.06    # per-tick chance an idle resource spoofs
    spoof_duration: int = 6            # ticks the phantom walls rest
    spoof_walls: int = 2               # layered phantom walls per cycle
    spoof_depth_fraction: float = 0.60 # each wall = 60% of venue capacity
    spoof_wall_price_mult: float = 1.6 # walls posted at 160% of mid (out-of-money)
    spoof_markup: float = 4.0          # +400% resale markup post-dip
    spoof_frenzy_ticks: int = 8        # monetization window after the crash
    spoof_cooldown_ticks: int = 40     # min ticks between manipulation cycles
    spoof_min_saturation: float = 0.35 # only spoof liquid books (>35% sat)
    dip_cash_fraction: float = 0.45    # max share of cash on the dip sweep

    # --- Reporting ---------------------------------------------------------
    report_ticks: int = 1000           # auto-generate tear sheet at this tick
    report_path: str = "backtest_report.md"

    # --- Risk / position limits ------------------------------------------
    starting_cash: float = 10_000.0
    max_position_per_resource: float = 500.0
    min_trade_size: float = 1.0
    slippage_bps: float = 25.0       # 25 bps of slippage on aggressive fills

    # --- Daemon loop ------------------------------------------------------
    tick_seconds: float = 1.5
    max_ticks: int = 0               # 0 == run until Ctrl-C

    @classmethod
    def from_env(cls) -> "StrategyConfig":
        """Allow overriding any field via NEXUS_<FIELD> env vars."""
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


# Resources traded on the Shared OS marketplace. Order matters for logging.
TRADED_RESOURCES = ("cpu_cores", "gpu_slices", "ram_pages", "bandwidth_mbps")
