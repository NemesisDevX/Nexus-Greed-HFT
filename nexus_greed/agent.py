"""Nexus-Greed trading daemon.

Owns the event loop, the agent's inventory and cash, and the profit/loss
ledger. Each tick it:
  1. advances the mock marketplace,
  2. asks the strategy for bid intents,
  3. routes them through the market client,
  4. books fills into the ledger,
  5. logs a one-line P&L summary to the console.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .config import TRADED_RESOURCES, StrategyConfig
from .market_client import SharedOSMarketClient, TradeFill
from .strategy import BidIntent, EpsilonGreedyStrategy

log = logging.getLogger("nexus_greed")

# A tick event callback: receives (snapshot, ledger_state dict, list of fills
# as (intent, fill) tuples). Runs in the trading thread, so implementations
# must be thread-safe (e.g. hand off via loop.call_soon_threadsafe).
TickCallback = Callable[[Dict, Dict, List[Tuple[BidIntent, TradeFill]]], None]


@dataclass
class Ledger:
    cash: float
    inventory: Dict[str, float] = field(default_factory=lambda: {r: 0.0 for r in TRADED_RESOURCES})
    realized_pnl: float = 0.0
    trades: List[TradeFill] = field(default_factory=list)
    # cost basis per resource for realized P&L on sells
    avg_cost: Dict[str, float] = field(default_factory=lambda: {r: 0.0 for r in TRADED_RESOURCES})

    def mark_to_market(self, snapshot) -> float:
        unreal = 0.0
        for r in TRADED_RESOURCES:
            unreal += self.inventory[r] * snapshot[r].mid_price
        return self.cash + unreal


class NexusGreedAgent:
    def __init__(
        self,
        cfg: StrategyConfig | None = None,
        seed: int | None = None,
        on_tick: Optional[TickCallback] = None,
    ) -> None:
        self.cfg = cfg or StrategyConfig.from_env()
        self.market = SharedOSMarketClient(seed=seed)
        self.strategy = EpsilonGreedyStrategy(self.cfg, rng=self.market._rng)
        self.ledger = Ledger(cash=self.cfg.starting_cash)
        self.starting_equity = self.cfg.starting_cash
        self.on_tick = on_tick
        # Predatory engine accounting: each SQUEEZE wall fill is one
        # counterparty forced to cross our markup; each CORNER sweep fill is
        # one completed cornering sequence.
        self.agents_squeezed = 0
        self.sweeps_executed = 0
        # Manipulation accounting: phantom walls posted, dips swept, and the
        # win/loss ledger for squeeze exits and dip rebounds.
        self.spoofs_placed = 0
        self.spoofs_cancelled = 0
        self.dips_bought = 0
        self.squeeze_wins = 0
        self.squeeze_losses = 0
        self.dip_wins = 0
        self.dip_losses = 0
        # Dip fills awaiting outcome evaluation once the frenzy window closes.
        self._pending_dips: deque = deque()
        # Rolling mark-to-market equity curve for VaR / Sharpe / drawdown.
        self._equity_curve: deque = deque(maxlen=8192)
        # Per-tick decide+execute latency samples (microseconds).
        self._lat_us: deque = deque(maxlen=20000)
        self._report_written = False

    # ------------------------------------------------------------------ #
    def _execute(self, intents: List[BidIntent]) -> List[Tuple[BidIntent, TradeFill]]:
        snap = self.market.snapshot()
        fills: List[Tuple[BidIntent, TradeFill]] = []
        for intent in intents:
            if intent.side == "HOLD":
                continue
            q = snap[intent.resource]
            if intent.side == "SPOOF":
                # Phantom wall: rests on the displayed book, never fills.
                self.market.spoof(intent.resource, intent.size,
                                  intent.limit_price,
                                  ttl=self.cfg.spoof_duration + 1)
                self.spoofs_placed += 1
                pseudo = TradeFill(intent.resource, "SPOOF", intent.size,
                                   intent.limit_price, self.market.tick)
                fills.append((intent, pseudo))
                log.info("SPOOF %-13s wall=%8.0fu @ %9.3f | %s",
                         intent.resource, intent.size, intent.limit_price,
                         intent.rationale)
                continue
            if intent.side == "CANCEL":
                n, tot = self.market.cancel_spoofs(
                    intent.resource, frenzy_ticks=self.cfg.spoof_frenzy_ticks)
                self.spoofs_cancelled += n
                pseudo = TradeFill(intent.resource, "CANCEL", tot,
                                   q.mid_price, self.market.tick)
                fills.append((intent, pseudo))
                log.info("CANCEL %-12s pulled %d walls (%.0fu) | %s",
                         intent.resource, n, tot, intent.rationale)
                continue
            if intent.side == "BUY":
                fill = self.market.buy(intent.resource, intent.size, intent.limit_price)
                if fill:
                    self._book_buy(fill)
                    self.strategy.learn_from_fill(intent, fill.price, q)
                    fills.append((intent, fill))
                    if intent.regime == "CORNER":
                        self.sweeps_executed += 1
                    elif intent.regime == "DIP":
                        self.dips_bought += 1
                        self._pending_dips.append(
                            {"tick": self.market.tick,
                             "resource": intent.resource,
                             "price": fill.price})
                    log.info(
                        "BUY  %-14s size=%7.2f @ %8.3f  | %s",
                        intent.resource, fill.size, fill.price, intent.rationale,
                    )
            else:  # SELL
                if intent.regime in ("HOARD", "SQUEEZE"):
                    # Passive marked-up offer: only clears when the book stays
                    # scarce and a desperate counterparty crosses our wall.
                    fill = self.market.hoard_sell(
                        intent.resource, intent.size, intent.limit_price,
                        self.cfg.scarcity_supply_pct,
                    )
                else:
                    fill = self.market.sell(intent.resource, intent.size, intent.limit_price)
                if fill:
                    pnl = self._book_sell(fill)
                    self.strategy.learn_from_fill(intent, fill.price, q)
                    fills.append((intent, fill))
                    if intent.regime == "SQUEEZE":
                        self.agents_squeezed += 1
                        if pnl > 0:
                            self.squeeze_wins += 1
                        else:
                            self.squeeze_losses += 1
                    log.info(
                        "SELL %-14s size=%7.2f @ %8.3f  pnl=%+8.2f | %s",
                        intent.resource, fill.size, fill.price, pnl, intent.rationale,
                    )
        return fills

    def _book_buy(self, fill: TradeFill) -> None:
        cost = fill.size * fill.price
        if cost > self.ledger.cash:
            # Trim to affordable (shouldn't normally happen, defensive only).
            fill.size = self.ledger.cash / fill.price
            cost = self.ledger.cash
        inv = self.ledger.inventory[fill.resource]
        new_inv = inv + fill.size
        if new_inv > 0:
            self.ledger.avg_cost[fill.resource] = (
                (self.ledger.avg_cost[fill.resource] * inv + fill.price * fill.size) / new_inv
            )
        self.ledger.inventory[fill.resource] = new_inv
        self.ledger.cash -= cost
        self.ledger.trades.append(fill)

    def _book_sell(self, fill: TradeFill) -> float:
        inv = self.ledger.inventory[fill.resource]
        fill.size = min(fill.size, inv)
        proceeds = fill.size * fill.price
        basis = fill.size * self.ledger.avg_cost[fill.resource]
        pnl = proceeds - basis
        self.ledger.inventory[fill.resource] = inv - fill.size
        self.ledger.cash += proceeds
        self.ledger.realized_pnl += pnl
        self.ledger.trades.append(fill)
        return pnl

    # ------------------------------------------------------------------ #
    def _risk_metrics(self, equity: float) -> Dict[str, float]:
        """Historical VaR(95) and annualized Sharpe over the equity curve.

        VaR(95) is the 5th-percentile per-tick return applied to current
        equity -- the expected loss on a bad tick. Sharpe is the per-tick
        mean/std of returns, annualized with the conventional sqrt(252)
        scaling (each tick treated as one trading period).
        """
        curve = list(self._equity_curve)
        rets = [(b - a) / a for a, b in zip(curve, curve[1:]) if a > 0]
        if len(rets) < 10:
            return {"var_95": 0.0, "sharpe": 0.0}
        ordered = sorted(rets)
        var_ret = ordered[max(0, int(0.05 * len(ordered)))]
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / len(rets)
        std = math.sqrt(var)
        sharpe = (mean / std) * math.sqrt(252.0) if std > 1e-12 else 0.0
        return {"var_95": abs(var_ret) * equity, "sharpe": sharpe}

    def _resolve_dips(self, snapshot: Dict) -> None:
        """Score dip buys once the post-spoof frenzy window has closed:
        a win is a dip fill whose mid has rebounded above entry."""
        horizon = self.cfg.spoof_frenzy_ticks
        keep = deque()
        while self._pending_dips:
            d = self._pending_dips.popleft()
            if self.market.tick - d["tick"] >= horizon:
                if snapshot[d["resource"]].mid_price > d["price"]:
                    self.dip_wins += 1
                else:
                    self.dip_losses += 1
            else:
                keep.append(d)
        self._pending_dips = keep

    # ------------------------------------------------------------------ #
    # Tear sheet
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pctile(xs: List[float], p: float) -> float:
        if not xs:
            return 0.0
        s = sorted(xs)
        return s[min(len(s) - 1, int(p * len(s)))]

    def _full_metrics(self) -> Dict[str, float]:
        """Institutional tear-sheet stats over the full equity curve."""
        curve = list(self._equity_curve)
        rets = [(b - a) / a for a, b in zip(curve, curve[1:]) if a > 0]
        m: Dict[str, float] = {"max_drawdown_pct": 0.0, "sortino": 0.0,
                               "calmar": 0.0, "sharpe": 0.0, "var_95": 0.0}
        # Max drawdown over the whole run.
        peak, max_dd = 0.0, 0.0
        for eq in curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)
        m["max_drawdown_pct"] = max_dd * 100.0
        if len(rets) < 10:
            return m
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / len(rets)
        std = math.sqrt(var)
        downside = [x for x in rets if x < 0]
        ddev = math.sqrt(sum(x * x for x in downside) / len(rets)) if downside else 0.0
        m["sharpe"] = (mean / std) * math.sqrt(252.0) if std > 1e-12 else 0.0
        m["sortino"] = (mean / ddev) * math.sqrt(252.0) if ddev > 1e-12 else float("inf")
        total_ret = (curve[-1] / curve[0] - 1.0) if curve and curve[0] > 0 else 0.0
        m["calmar"] = total_ret / max_dd if max_dd > 1e-9 else float("inf")
        ordered = sorted(rets)
        m["var_95"] = abs(ordered[max(0, int(0.05 * len(ordered)))]) * curve[-1]
        return m

    def _write_report(self) -> None:
        """Emit backtest_report.md — the quant tear sheet."""
        snap = self.market.snapshot()
        equity = self.ledger.mark_to_market(snap)
        roi = (equity - self.starting_equity) / self.starting_equity * 100.0
        m = self._full_metrics()
        lats = list(self._lat_us)
        n_squeeze = self.squeeze_wins + self.squeeze_losses
        n_dips = self.dip_wins + self.dip_losses

        def fin(x: float) -> str:
            return "inf" if x == float("inf") else f"{x:.2f}"

        lines = [
            "# NEXUS-GREED — Backtest Tear Sheet",
            "",
            f"_Auto-generated at tick {self.market.tick} — "
            "Predatory Liquidity Engine + Spoof & Layer warfare._",
            "",
            "## Performance",
            "",
            "| Metric | Value |",
            "|---|---:|",
            f"| Ticks | {self.market.tick} |",
            f"| Total fills | {len(self.ledger.trades)} |",
            f"| Ending equity (MTM) | {equity:,.2f} |",
            f"| Realized P&L | {self.ledger.realized_pnl:+,.2f} |",
            f"| ROI | {roi:+,.2f}% |",
            f"| Sharpe (√252 ann.) | {fin(m['sharpe'])} |",
            f"| Sortino (√252 ann.) | {fin(m['sortino'])} |",
            f"| Calmar (ret/maxDD) | {fin(m['calmar'])} |",
            f"| Max drawdown | {m['max_drawdown_pct']:.3f}% |",
            f"| VaR(95) / tick | {m['var_95']:,.2f} |",
            "",
            "## Execution Latency (decide + route, µs)",
            "",
            "| p50 | p95 | p99 | mean |",
            "|---:|---:|---:|---:|",
            f"| {self._pctile(lats, 0.50):.1f} | {self._pctile(lats, 0.95):.1f} "
            f"| {self._pctile(lats, 0.99):.1f} "
            f"| {(sum(lats) / len(lats)) if lats else 0.0:.1f} |",
            "",
            "## Warfare Statistics",
            "",
            "| Metric | Value |",
            "|---|---:|",
            f"| Cornering sweeps | {self.sweeps_executed} |",
            f"| Agents squeezed | {self.agents_squeezed} |",
            f"| Squeeze fills W/L | {self.squeeze_wins}/{self.squeeze_losses} "
            f"({(self.squeeze_wins / n_squeeze * 100) if n_squeeze else 0.0:.1f}% win) |",
            f"| Spoof walls placed | {self.spoofs_placed} |",
            f"| Spoof walls cancelled | {self.spoofs_cancelled} |",
            f"| Dips bought | {self.dips_bought} |",
            f"| Dip outcomes W/L | {self.dip_wins}/{self.dip_losses} "
            f"({(self.dip_wins / n_dips * 100) if n_dips else 0.0:.1f}% win) |",
            "",
            "## Final Inventory",
            "",
            "| Resource | Held | Avg cost | Mid |",
            "|---|---:|---:|---:|",
        ]
        for r in TRADED_RESOURCES:
            lines.append(
                f"| {r} | {self.ledger.inventory[r]:,.2f} "
                f"| {self.ledger.avg_cost[r]:.3f} | {snap[r].mid_price:.3f} |")
        lines += [
            "",
            "## Configuration",
            "",
            "```",
            *(f"{k} = {v}" for k, v in self.cfg.__dict__.items()),
            "```",
            "",
        ]
        path = self.cfg.report_path
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        log.info("tear sheet written -> %s", path)
        self._report_written = True

    def ledger_state(self, snapshot: Dict) -> Dict:
        """Snapshot the ledger into a JSON-serializable dict for streaming."""
        equity = self.ledger.mark_to_market(snapshot)
        unreal = equity - self.ledger.cash
        roi = (equity - self.starting_equity) / self.starting_equity * 100.0
        risk = self._risk_metrics(equity)
        return {
            "tick": self.market.tick,
            "cash": round(self.ledger.cash, 4),
            "equity": round(equity, 4),
            "realized_pnl": round(self.ledger.realized_pnl, 4),
            "unrealized_pnl": round(unreal, 4),
            "roi_pct": round(roi, 4),
            "starting_equity": round(self.starting_equity, 4),
            "epsilon": round(self.strategy.epsilon, 5),
            "inventory": {r: round(self.ledger.inventory[r], 4) for r in TRADED_RESOURCES},
            "avg_cost": {r: round(self.ledger.avg_cost[r], 4) for r in TRADED_RESOURCES},
            "num_trades": len(self.ledger.trades),
            "var_95": round(risk["var_95"], 4),
            "sharpe": round(risk["sharpe"], 4),
            "agents_squeezed": self.agents_squeezed,
            "sweeps_executed": self.sweeps_executed,
            "spoofs_placed": self.spoofs_placed,
            "dips_bought": self.dips_bought,
            "squeeze_win_rate": round(
                self.squeeze_wins / max(1, self.squeeze_wins + self.squeeze_losses) * 100.0, 2),
        }

    # ------------------------------------------------------------------ #
    def _log_summary(self) -> None:
        snap = self.market.snapshot()
        equity = self.ledger.mark_to_market(snap)
        roi = (equity - self.starting_equity) / self.starting_equity * 100.0
        # Find the tightest market right now for colour.
        tightest = min(snap.values(), key=lambda q: q.market_saturation)
        log.info(
            "TICK %04d | equity=%10.2f  cash=%9.2f  realized=%+8.2f  ROI=%+5.2f%%  "
            "eps=%.3f | squeezed=%d sweeps=%d | scarcest=%s(%.0f%%)",
            self.market.tick, equity, self.ledger.cash, self.ledger.realized_pnl,
            roi, self.strategy.epsilon, self.agents_squeezed, self.sweeps_executed,
            tightest.resource, tightest.market_saturation * 100,
        )

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        log.info("Nexus-Greed online. starting_cash=%.2f resources=%s",
                 self.cfg.starting_cash, ", ".join(TRADED_RESOURCES))
        try:
            while self.cfg.max_ticks == 0 or self.market.tick < self.cfg.max_ticks:
                self.market.advance()
                snap = self.market.snapshot()
                self.strategy.observe(snap)
                self._resolve_dips(snap)
                t0 = time.perf_counter_ns()
                intents = self.strategy.decide(snap, self.ledger.inventory, self.ledger.cash)
                fills = self._execute(intents)
                self._lat_us.append((time.perf_counter_ns() - t0) / 1000.0)
                # Equity curve is owned by the tick loop itself — headless
                # backtests need it too, not just streaming subscribers.
                self._equity_curve.append(self.ledger.mark_to_market(snap))
                self._log_summary()
                if (not self._report_written
                        and self.market.tick >= self.cfg.report_ticks):
                    self._write_report()
                if self.on_tick is not None:
                    self.on_tick(snap, self.ledger_state(snap), fills)
                time.sleep(self.cfg.tick_seconds)
        except KeyboardInterrupt:
            log.info("Nexus-Greed shutting down on interrupt.")
        finally:
            self._final_report()

    def _final_report(self) -> None:
        snap = self.market.snapshot()
        equity = self.ledger.mark_to_market(snap)
        roi = (equity - self.starting_equity) / self.starting_equity * 100.0
        log.info("=" * 64)
        log.info("FINAL REPORT  |  ticks=%d  trades=%d", self.market.tick, len(self.ledger.trades))
        log.info("  cash      : %10.2f", self.ledger.cash)
        log.info("  realized   : %+10.2f", self.ledger.realized_pnl)
        log.info("  equity(MTM): %10.2f", equity)
        log.info("  ROI        : %+.2f%%", roi)
        log.info("  sweeps=%d  agents_squeezed=%d  spoofs=%d/%d  dips=%d (W/L %d/%d)",
                 self.sweeps_executed, self.agents_squeezed,
                 self.spoofs_placed, self.spoofs_cancelled,
                 self.dips_bought, self.dip_wins, self.dip_losses)
        if not self._report_written:
            self._write_report()
        for r in TRADED_RESOURCES:
            log.info("  %-13s held=%7.2f  avg_cost=%.3f  mid=%.3f",
                     r, self.ledger.inventory[r], self.ledger.avg_cost[r], snap[r].mid_price)
        log.info("=" * 64)
