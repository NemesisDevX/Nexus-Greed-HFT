# NEXUS-GREED — Backtest Tear Sheet

_Auto-generated at tick 1000 — Predatory Liquidity Engine + Spoof & Layer warfare._

## Performance

| Metric | Value |
|---|---:|
| Ticks | 1000 |
| Total fills | 11398 |
| Ending equity (MTM) | 5,114,168.27 |
| Realized P&L | +5,104,190.08 |
| ROI | +51,041.68% |
| Sharpe (√252 ann.) | 5.33 |
| Sortino (√252 ann.) | 408.26 |
| Calmar (ret/maxDD) | 27642.96 |
| Max drawdown | 1.489% |
| VaR(95) / tick | 3,345.73 |

## Execution Latency (decide + route, µs)

| p50 | p95 | p99 | mean |
|---:|---:|---:|---:|
| 5483.6 | 41895.8 | 128695.6 | 12103.1 |

## Warfare Statistics

| Metric | Value |
|---|---:|
| Cornering sweeps | 2642 |
| Agents squeezed | 8419 |
| Squeeze fills W/L | 8419/0 (100.0% win) |
| Spoof walls placed | 38 |
| Spoof walls cancelled | 38 |
| Dips bought | 15 |
| Dip outcomes W/L | 15/0 (100.0% win) |

## Final Inventory

| Resource | Held | Avg cost | Mid |
|---|---:|---:|---:|
| cpu_cores | 70.19 | 11.784 | 11.688 |
| gpu_slices | 33.82 | 41.573 | 41.396 |
| ram_pages | 0.00 | 4.032 | 4.195 |
| bandwidth_mbps | 169.56 | 2.126 | 2.072 |

## Configuration

```
epsilon = 0.1
epsilon_decay = 0.995
epsilon_floor = 0.02
ma_window = 12
buy_z_threshold = -1.2
sell_z_threshold = 1.6
scarcity_supply_pct = 0.18
hoard_markup = 3.0
hoard_max_fraction = 0.45
obi_weight = 0.5
cornering_cash_fraction = 0.35
sweep_depth_fraction = 1.0
squeeze_wall_levels = 3
squeeze_wall_step = 0.1
spoof_enabled = True
spoof_probability = 0.06
spoof_duration = 6
spoof_walls = 2
spoof_depth_fraction = 0.6
spoof_wall_price_mult = 1.6
spoof_markup = 4.0
spoof_frenzy_ticks = 8
spoof_cooldown_ticks = 40
spoof_min_saturation = 0.35
dip_cash_fraction = 0.45
report_ticks = 1000
report_path = backtest_report.md
starting_cash = 10000.0
max_position_per_resource = 500.0
min_trade_size = 1.0
slippage_bps = 25.0
tick_seconds = 0.06
max_ticks = 0
```
