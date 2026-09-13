"""Policy engine. Three regimes, priority order:

  manip FSM (SPOOF->CANCEL->DIP->DISTRIBUTE) > cornering (sat<18% =>
  sweep+wall ladder) > epsilon-greedy z-score arb (OBI-tilted trigger)

Tabular Q, EMA updates — no net, no pipeline, fast enough.
"""
from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Tuple

from .config import TRADED_RESOURCES, StrategyConfig
from .market_client import Quote, SharedOSMarketClient


@dataclass
class ResourceState:
    price_history: Deque[float] = field(default_factory=lambda: deque(maxlen=64))
    obi_history: Deque[float] = field(default_factory=lambda: deque(maxlen=64))
    q_buy: float = 0.0
    q_sell: float = 0.0
    last_buy_tick: int = -1
    last_buy_price: float = 0.0
    # manip FSM
    manip_state: str = "IDLE"   # IDLE | SPOOFING | DISTRIBUTE
    spoof_ticks_left: int = 0
    dist_ticks_left: int = 0
    spoof_cooldown: int = 0


@dataclass
class BidIntent:
    resource: str
    side: str               # BUY|SELL|HOLD|SPOOF|CANCEL
    size: float
    limit_price: float
    rationale: str
    exploring: bool = False
    regime: str = "ARB"     # ARB|CORNER|SQUEEZE|EXPLORE|SPOOF|CANCEL|DIP|HOLD


class EpsilonGreedyStrategy:
    def __init__(self, cfg: StrategyConfig, rng: random.Random | None = None) -> None:
        self.cfg = cfg
        self._rng = rng or random.Random()
        self.epsilon = cfg.epsilon
        self.state: Dict[str, ResourceState] = {r: ResourceState() for r in TRADED_RESOURCES}

    # ------------------------------------------------------------------ #
    # signal
    # ------------------------------------------------------------------ #
    def _rolling_stats(self, resource: str) -> Tuple[float, float]:
        hist = list(self.state[resource].price_history)
        n = len(hist)
        if n < 2:
            return (hist[-1] if hist else 0.0, 0.0)
        window = hist[-self.cfg.ma_window :]
        mean = sum(window) / len(window)
        var = sum((p - mean) ** 2 for p in window) / len(window)
        return mean, math.sqrt(var)

    def _zscore(self, resource: str, price: float) -> float:
        mean, std = self._rolling_stats(resource)
        if std <= 1e-9:
            return 0.0
        return (price - mean) / std

    # ------------------------------------------------------------------ #
    # tabular Q, EMA
    # ------------------------------------------------------------------ #
    def _update_q(self, resource: str, reward: float, side: str) -> None:
        alpha = 0.15
        st = self.state[resource]
        if side == "BUY":
            st.q_buy = (1 - alpha) * st.q_buy + alpha * reward
        else:
            st.q_sell = (1 - alpha) * st.q_sell + alpha * reward

    def _reward_buy(self, resource: str, quote: Quote) -> float:
        mean, _ = self._rolling_stats(resource)
        if mean <= 0:
            return 0.0
        return (mean - quote.best_ask) / mean

    def _reward_sell(self, resource: str, fill_price: float, quote: Quote) -> float:
        if quote.mid_price <= 0:
            return 0.0
        return (fill_price - quote.mid_price) / quote.mid_price

    # ------------------------------------------------------------------ #
    # decide
    # ------------------------------------------------------------------ #
    def observe(self, snapshot: Dict[str, Quote]) -> None:
        for r, q in snapshot.items():
            self.state[r].price_history.append(q.mid_price)
            self.state[r].obi_history.append(q.obi)
            st = self.state[r]
            if st.last_buy_tick >= 0 and q.last_tick - st.last_buy_tick >= 1:
                # settle last buy's reward on reversion
                reversion = (q.mid_price - st.last_buy_price) / max(st.last_buy_price, 1e-9)
                self._update_q(r, reversion, "BUY")
                st.last_buy_tick = -1

    # ------------------------------------------------------------------ #
    # cornering
    # ------------------------------------------------------------------ #
    def _cornering_intents(self, resource: str, q: Quote, inv: float,
                           cash: float, st: ResourceState) -> List[BidIntent]:
        out: List[BidIntent] = []

        # SWEEP: lift what's left of the book
        headroom = max(0.0, self.cfg.max_position_per_resource - inv)
        affordable = (cash * self.cfg.cornering_cash_fraction) / q.best_ask if q.best_ask > 0 else 0.0
        sweep_size = min(q.supply * self.cfg.sweep_depth_fraction, headroom, affordable)
        post_sweep_inv = inv
        if sweep_size >= self.cfg.min_trade_size:
            out.append(BidIntent(
                resource=resource, side="BUY", size=sweep_size,
                limit_price=q.best_ask * 1.02,
                rationale=f"CORNER sweep: lifted {sweep_size:.1f} depth "
                          f"(sat={q.market_saturation:.0%} obi={q.obi:+.2f})",
                regime="CORNER",
            ))
            post_sweep_inv += sweep_size
            st.last_buy_tick = q.last_tick
            st.last_buy_price = q.best_ask

        # SQUEEZE: pin the exit with a wall ladder
        if post_sweep_inv >= self.cfg.min_trade_size:
            out.extend(self._wall_intents(resource, q, post_sweep_inv,
                                        self.cfg.hoard_markup))

        if not out:
            out.append(BidIntent(resource=resource, side="HOLD", size=0.0,
                                 limit_price=0.0, rationale="corner: no ammo",
                                 regime="HOLD"))
        return out

    def _wall_intents(self, resource: str, q: Quote, inv: float,
                      markup: float) -> List[BidIntent]:
        levels = max(1, self.cfg.squeeze_wall_levels)
        wall_budget = inv * self.cfg.hoard_max_fraction
        per_wall = max(self.cfg.min_trade_size, wall_budget / levels)
        out: List[BidIntent] = []
        for lvl in range(levels):
            wall_price = (q.mid_price * markup
                          * (1.0 + lvl * self.cfg.squeeze_wall_step))
            out.append(BidIntent(
                resource=resource, side="SELL", size=per_wall,
                limit_price=wall_price,
                rationale=f"SQUEEZE wall L{lvl + 1} @ {wall_price:.2f} "
                          f"(+{markup:.0%} markup sat={q.market_saturation:.0%})",
                regime="SQUEEZE",
            ))
        return out

    # ------------------------------------------------------------------ #
    # spoof machine
    # ------------------------------------------------------------------ #
    def _manipulate(self, r: str, q: Quote, inv: float, cash: float,
                    st: ResourceState) -> List[BidIntent]:
        if st.manip_state == "SPOOFING":
            st.spoof_ticks_left -= 1
            if st.spoof_ticks_left > 0:
                return [BidIntent(resource=r, side="HOLD", size=0.0,
                                  limit_price=0.0,
                                  rationale=f"SPOOF resting ({st.spoof_ticks_left}t)",
                                  regime="HOLD")]
            # dip first, pull walls second — fill prints at the depressed ask
            intents: List[BidIntent] = []
            dip_size = min(
                q.supply * self.cfg.sweep_depth_fraction,
                max(0.0, self.cfg.max_position_per_resource - inv),
                (cash * self.cfg.dip_cash_fraction) / q.best_ask if q.best_ask > 0 else 0.0,
            )
            if dip_size >= self.cfg.min_trade_size:
                intents.append(BidIntent(
                    resource=r, side="BUY", size=dip_size,
                    limit_price=q.best_ask * 1.03,
                    rationale=f"DIP BOUGHT: swept {dip_size:.1f} of panic "
                              f"(obi={q.obi:+.2f})",
                    regime="DIP"))
                st.last_buy_tick = q.last_tick
                st.last_buy_price = q.best_ask
            intents.append(BidIntent(
                resource=r, side="CANCEL", size=0.0, limit_price=0.0,
                rationale=f"SPOOF CANCELLED — {self.cfg.spoof_walls} phantom "
                          f"walls pulled @ {q.mid_price:.2f}",
                regime="CANCEL"))
            st.manip_state = "DISTRIBUTE"
            st.dist_ticks_left = self.cfg.spoof_frenzy_ticks
            return intents

        # DISTRIBUTE: dump the dip inventory at +400%
        st.dist_ticks_left -= 1
        if inv >= self.cfg.min_trade_size:
            out = self._wall_intents(r, q, inv, self.cfg.spoof_markup)
        else:
            out = [BidIntent(resource=r, side="HOLD", size=0.0, limit_price=0.0,
                             rationale="distribute: flat", regime="HOLD")]
        if st.dist_ticks_left <= 0:
            st.manip_state = "IDLE"
            st.spoof_cooldown = self.cfg.spoof_cooldown_ticks
        return out

    def _maybe_start_spoof(self, r: str, q: Quote, cash: float,
                           st: ResourceState) -> List[BidIntent] | None:
        # needs a liquid book (no one left to panic on a dead one),
        # no cooldown, some cash, and the dice roll
        if not self.cfg.spoof_enabled:
            return None
        if st.spoof_cooldown > 0:
            return None
        if q.market_saturation < self.cfg.spoof_min_saturation:
            return None
        if cash < self.cfg.starting_cash * 0.05:
            return None
        if self._rng.random() >= self.cfg.spoof_probability:
            return None

        st.manip_state = "SPOOFING"
        st.spoof_ticks_left = self.cfg.spoof_duration
        intents: List[BidIntent] = []
        for lvl in range(self.cfg.spoof_walls):
            wall_price = (q.mid_price * self.cfg.spoof_wall_price_mult
                          * (1.0 + lvl * self.cfg.squeeze_wall_step))
            wall_size = q.capacity * self.cfg.spoof_depth_fraction
            intents.append(BidIntent(
                resource=r, side="SPOOF", size=wall_size,
                limit_price=wall_price,
                rationale=f"SPOOF wall {wall_size:.0f}u @ {wall_price:.2f} "
                          f"({self.cfg.spoof_wall_price_mult:.0%} of mid)",
                regime="SPOOF"))
        return intents

    # ------------------------------------------------------------------ #
    # Decision making
    # ------------------------------------------------------------------ #
    def decide(self, snapshot: Dict[str, Quote], inventory: Dict[str, float], cash: float) -> List[BidIntent]:
        intents: List[BidIntent] = []
        cash_remaining = cash
        for r in TRADED_RESOURCES:
            q = snapshot[r]
            z = self._zscore(r, q.mid_price)
            obi = q.obi
            # OBI tilts the trigger — lean into bid crowding
            z_eff = z - self.cfg.obi_weight * obi
            sat = q.market_saturation
            st = self.state[r]
            if st.spoof_cooldown > 0:
                st.spoof_cooldown -= 1

            if st.manip_state != "IDLE":
                manip = self._manipulate(
                    r, q, inventory.get(r, 0.0), cash_remaining, st)
                intents.extend(manip)
                cash_remaining -= sum(
                    it.size * it.limit_price for it in manip if it.side == "BUY")
                continue

            # cornerable book beats everything else on the menu
            if sat < self.cfg.scarcity_supply_pct:
                corner = self._cornering_intents(
                    r, q, inventory.get(r, 0.0), cash_remaining, st)
                intents.extend(corner)
                cash_remaining -= sum(
                    it.size * it.limit_price for it in corner if it.side == "BUY")
                continue

            spoof = self._maybe_start_spoof(r, q, cash_remaining, st)
            if spoof is not None:
                intents.extend(spoof)
                continue

            # explore
            if self._rng.random() < self.epsilon:
                probe_size = self.cfg.min_trade_size * self._rng.randint(1, 5)
                if self._rng.random() < 0.5 and cash_remaining > probe_size * q.best_ask:
                    intents.append(BidIntent(
                        resource=r, side="BUY", size=probe_size,
                        limit_price=q.best_ask * 1.01,
                        rationale=f"EXPLORE probe-buy z={z:+.2f} obi={obi:+.2f}",
                        exploring=True, regime="EXPLORE",
                    ))
                elif inventory.get(r, 0.0) > probe_size:
                    intents.append(BidIntent(
                        resource=r, side="SELL", size=probe_size,
                        limit_price=q.best_bid * 0.99,
                        rationale=f"EXPLORE probe-sell z={z:+.2f} obi={obi:+.2f}",
                        exploring=True, regime="EXPLORE",
                    ))
                continue

            # exploit
            if z_eff < self.cfg.buy_z_threshold and cash_remaining > q.best_ask * self.cfg.min_trade_size:
                conviction = min(1.0, abs(z_eff) / 3.0)
                bid_vol = min(
                    self.cfg.max_position_per_resource - inventory.get(r, 0.0),
                    self.cfg.max_position_per_resource * conviction,
                    cash_remaining / q.best_ask * 0.4,
                )
                bid_vol = max(bid_vol, self.cfg.min_trade_size)
                intents.append(BidIntent(
                    resource=r, side="BUY", size=bid_vol,
                    limit_price=q.best_ask * 1.005,
                    rationale=f"ARB buy z={z:+.2f} obi={obi:+.2f} below MA (conv={conviction:.0%})",
                ))
                cash_remaining -= bid_vol * q.best_ask * 1.005
                st.last_buy_tick = q.last_tick
                st.last_buy_price = q.best_ask
                continue

            if z_eff > self.cfg.sell_z_threshold and inventory.get(r, 0.0) > 0:
                ask_vol = min(inventory[r], self.cfg.max_position_per_resource * 0.3)
                intents.append(BidIntent(
                    resource=r, side="SELL", size=ask_vol,
                    limit_price=q.best_bid * 0.995,
                    rationale=f"ARB sell z={z:+.2f} obi={obi:+.2f} above MA",
                ))
                continue

            intents.append(BidIntent(resource=r, side="HOLD", size=0.0,
                                      limit_price=0.0, rationale="no edge",
                                      regime="HOLD"))

        self.epsilon = max(self.cfg.epsilon_floor, self.epsilon * self.cfg.epsilon_decay)
        return intents

    def learn_from_fill(self, intent: BidIntent, fill_price: float, quote: Quote) -> None:
        if intent.side == "BUY":
            if intent.regime in ("CORNER", "DIP"):
                # reward vs the wall exit, not the mid — the squeeze is the trade
                markup = (self.cfg.spoof_markup if intent.regime == "DIP"
                          else self.cfg.hoard_markup)
                exit_target = quote.mid_price * markup
                reward = (exit_target - fill_price) / max(exit_target, 1e-9)
            else:
                reward = self._reward_buy(intent.resource, quote)
            self._update_q(intent.resource, reward, "BUY")
        elif intent.side == "SELL":
            reward = self._reward_sell(intent.resource, fill_price, quote)
            self._update_q(intent.resource, reward, "SELL")
