"""Epsilon-Greedy RL bidding engine + Predatory Liquidity Engine.

Two coupled decision layers:

1. **Predatory Liquidity Engine (dominant regime).** Every tick the agent
   reads the Level-2 book and computes real-time Order Book Imbalance (OBI):

       OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)   in [-1, +1]

   When market saturation collapses below the scarcity threshold (18% of
   venue capacity), the engine executes a **Market Cornering** sequence:

     a. SWEEP -- a single aggressive marketable order that lifts all
        remaining ask-side depth, removing the last visible liquidity.
     b. SQUEEZE -- a ladder of passive Limit Sell walls posted at a +300%
        markup over fair value. Competing agents whose workloads still need
        the resource are forced to cross our walls at the squeeze price.

   Each wall fill is counted as one squeezed counterparty.

2. **Market Manipulation (spoof & layering).** On liquid books the engine
   periodically posts massive out-of-the-money phantom sell walls
   (SPOOFING). The fake supply drags the tape down; when the dip matures the
   walls are pulled (CANCEL) and the depressed book is swept (DIP buy).
   During the post-spoof demand frenzy the inventory is resold at a +400%
   markup (DISTRIBUTE).

3. **Epsilon-Greedy RL layer (default regime).** Per-resource tabular
   Q-values for the aggressive BUY/SELL actions, updated by EMA toward
   shaped rewards. With probability epsilon the agent fires a small probe
   order to gather signal; otherwise it exploits the z-score arbitrage
   signal, tilted by live OBI (a bid-heavy book lowers the effective
   z-threshold to buy; an offer-heavy book raises it).

Reward shaping:
  * Arbitrage buys below the moving average are rewarded on reversion.
  * CORNER sweeps are rewarded against their expected exit at the wall
    price (mid * markup), not the current mid -- the sweep is justified by
    the squeeze, not by reversion.
  * SQUEEZE walls rewarded by realized markup over fair value.

This is a mock RL loop -- no neural net, just tabular Q-updates with an
exponential moving average. That is enough to demonstrate adaptive bidding
behaviour for the hackathon without a training pipeline.
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
    q_buy: float = 0.0      # estimated value of an aggressive BUY action
    q_sell: float = 0.0     # estimated value of an aggressive SELL action
    last_buy_tick: int = -1
    last_buy_price: float = 0.0
    # --- Market-manipulation state machine --------------------------------
    manip_state: str = "IDLE"   # "IDLE" | "SPOOFING" | "DISTRIBUTE"
    spoof_ticks_left: int = 0   # phantom walls rest this many more ticks
    dist_ticks_left: int = 0    # squeeze-distribution window remaining
    spoof_cooldown: int = 0     # ticks until this resource can be spoofed again


@dataclass
class BidIntent:
    """A signed trading intention produced by the strategy."""
    resource: str
    side: str               # "BUY" | "SELL" | "HOLD" | "SPOOF" | "CANCEL"
    size: float
    limit_price: float
    rationale: str          # human-readable reason, logged to console
    exploring: bool = False
    regime: str = "ARB"     # ARB|CORNER|SQUEEZE|EXPLORE|SPOOF|CANCEL|DIP|HOLD


class EpsilonGreedyStrategy:
    def __init__(self, cfg: StrategyConfig, rng: random.Random | None = None) -> None:
        self.cfg = cfg
        self._rng = rng or random.Random()
        self.epsilon = cfg.epsilon
        self.state: Dict[str, ResourceState] = {r: ResourceState() for r in TRADED_RESOURCES}

    # ------------------------------------------------------------------ #
    # Signal extraction
    # ------------------------------------------------------------------ #
    def _rolling_stats(self, resource: str) -> Tuple[float, float]:
        """Return (moving_average, std) of recent mid-prices."""
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
    # Q-learning update (tabular, exponential moving average)
    # ------------------------------------------------------------------ #
    def _update_q(self, resource: str, reward: float, side: str) -> None:
        alpha = 0.15  # learning rate
        st = self.state[resource]
        if side == "BUY":
            st.q_buy = (1 - alpha) * st.q_buy + alpha * reward
        else:
            st.q_sell = (1 - alpha) * st.q_sell + alpha * reward

    def _reward_buy(self, resource: str, quote: Quote) -> float:
        """Reward = how far below fair value we bought (reversion upside)."""
        mean, _ = self._rolling_stats(resource)
        if mean <= 0:
            return 0.0
        return (mean - quote.best_ask) / mean  # positive when we bought cheap

    def _reward_sell(self, resource: str, fill_price: float, quote: Quote) -> float:
        """Reward = realized markup over fair value."""
        if quote.mid_price <= 0:
            return 0.0
        return (fill_price - quote.mid_price) / quote.mid_price

    # ------------------------------------------------------------------ #
    # Decision making
    # ------------------------------------------------------------------ #
    def observe(self, snapshot: Dict[str, Quote]) -> None:
        for r, q in snapshot.items():
            self.state[r].price_history.append(q.mid_price)
            self.state[r].obi_history.append(q.obi)
            # Close out the previous buy's reward using the new mid-price.
            st = self.state[r]
            if st.last_buy_tick >= 0 and q.last_tick - st.last_buy_tick >= 1:
                # Pseudo-reward: did the price revert up since we bought?
                reversion = (q.mid_price - st.last_buy_price) / max(st.last_buy_price, 1e-9)
                self._update_q(r, reversion, "BUY")
                st.last_buy_tick = -1

    # ------------------------------------------------------------------ #
    # Predatory Liquidity Engine
    # ------------------------------------------------------------------ #
    def _cornering_intents(self, resource: str, q: Quote, inv: float,
                           cash: float, st: ResourceState) -> List[BidIntent]:
        """Market Cornering sequence for one scarce resource.

        a. SWEEP -- lift all remaining ask-side depth in one marketable order.
        b. SQUEEZE -- post a ladder of Limit Sell walls at the hoard markup
           so competing agents must cross our price to get the resource.
        """
        out: List[BidIntent] = []

        # a. SWEEP — buy out all remaining depth, bounded by position cap
        #    and the per-sweep cash budget.
        headroom = max(0.0, self.cfg.max_position_per_resource - inv)
        affordable = (cash * self.cfg.cornering_cash_fraction) / q.best_ask if q.best_ask > 0 else 0.0
        sweep_size = min(q.supply * self.cfg.sweep_depth_fraction, headroom, affordable)
        post_sweep_inv = inv
        if sweep_size >= self.cfg.min_trade_size:
            out.append(BidIntent(
                resource=resource, side="BUY", size=sweep_size,
                limit_price=q.best_ask * 1.02,  # cross the spread, take the book
                rationale=f"CORNER sweep: lifted {sweep_size:.1f} depth "
                          f"(sat={q.market_saturation:.0%} obi={q.obi:+.2f})",
                regime="CORNER",
            ))
            post_sweep_inv += sweep_size
            st.last_buy_tick = q.last_tick
            st.last_buy_price = q.best_ask

        # b. SQUEEZE — laddered Limit Sell walls at markup * (1 + lvl*step).
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
        """Laddered Limit Sell walls at `markup` × mid, stepped per level."""
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
    # Market Manipulation: spoof & layering state machine
    # ------------------------------------------------------------------ #
    def _manipulate(self, r: str, q: Quote, inv: float, cash: float,
                    st: ResourceState) -> List[BidIntent]:
        """SPOOF -> CANCEL -> DIP -> DISTRIBUTE cycle for one resource.

        SPOOFING   : phantom walls rest on the book, dragging the tape down.
        cancel tick: pull every wall (CANCEL), then sweep the depressed book
                     with a DIP buy before the rebound.
        DISTRIBUTE : during the post-spoof frenzy, resell the dip inventory
                     at the spoof_markup (+400%) via squeeze walls.
        """
        if st.manip_state == "SPOOFING":
            st.spoof_ticks_left -= 1
            if st.spoof_ticks_left > 0:
                return [BidIntent(resource=r, side="HOLD", size=0.0,
                                  limit_price=0.0,
                                  rationale=f"SPOOF resting ({st.spoof_ticks_left}t)",
                                  regime="HOLD")]
            # FLASH CRASH EXPLOIT — buy the panic dip *before* the walls are
            # pulled, so the fill prints at the spoof-depressed ask.
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

        # DISTRIBUTE — resell the dip-bought inventory at +400%.
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
        """Decide whether to launch a fresh spoof cycle on an idle resource.

        Preconditions: a liquid book (spoofing an already-thin market is
        pointless — there's no one left to panic), no cooldown, and the
        per-tick probability roll. Returns SPOOF intents or None.
        """
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
            # OBI tilts the trigger: a bid-heavy book (obi > 0) lowers the
            # effective z so we lean into demand pressure; an offer-heavy
            # book does the opposite.
            z_eff = z - self.cfg.obi_weight * obi
            sat = q.market_saturation
            st = self.state[r]
            if st.spoof_cooldown > 0:
                st.spoof_cooldown -= 1

            # --- Market manipulation (active cycle dominates) -------------
            if st.manip_state != "IDLE":
                manip = self._manipulate(
                    r, q, inventory.get(r, 0.0), cash_remaining, st)
                intents.extend(manip)
                cash_remaining -= sum(
                    it.size * it.limit_price for it in manip if it.side == "BUY")
                continue

            # --- Predatory regime: Market Cornering ----------------------
            # Saturation below the scarcity floor => drain the book, then
            # re-offer at the squeeze markup. This layer dominates epsilon
            # exploration -- when the market is cornerable, we pounce.
            if sat < self.cfg.scarcity_supply_pct:
                corner = self._cornering_intents(
                    r, q, inventory.get(r, 0.0), cash_remaining, st)
                intents.extend(corner)
                # Reserve the sweep's cash so later resources can't
                # double-spend it within the same tick.
                cash_remaining -= sum(
                    it.size * it.limit_price for it in corner if it.side == "BUY")
                continue

            # --- Spoof launch: panic a liquid book -------------------------
            spoof = self._maybe_start_spoof(r, q, cash_remaining, st)
            if spoof is not None:
                intents.extend(spoof)
                continue

            # --- Exploration branch --------------------------------------
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

            # --- Exploitation branch -------------------------------------
            # Arbitrage: price below MA => buy aggressively.
            if z_eff < self.cfg.buy_z_threshold and cash_remaining > q.best_ask * self.cfg.min_trade_size:
                conviction = min(1.0, abs(z_eff) / 3.0)  # deeper discount => bigger bid
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

            # Arbitrage: price above MA and we hold inventory => sell.
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

        # Decay exploration rate.
        self.epsilon = max(self.cfg.epsilon_floor, self.epsilon * self.cfg.epsilon_decay)
        return intents

    def learn_from_fill(self, intent: BidIntent, fill_price: float, quote: Quote) -> None:
        """Update Q-values from realized fills."""
        if intent.side == "BUY":
            if intent.regime in ("CORNER", "DIP"):
                # A sweep/dip buy is rewarded against its expected exit at the
                # wall price, not the current mid -- the squeeze justifies it.
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
