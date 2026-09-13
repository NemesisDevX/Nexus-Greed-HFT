"""Mock Shared OS marketplace API client.

In production this would talk to the Shared OS A2A order book over gRPC. For the
hackathon we simulate an adversarial market: mean-reverting prices around a
drifting fair value, occasional supply shocks, and a live order book that
fills our bids/asks with realistic slippage.

The client is intentionally stateful so the agent sees a *consistent* market
across a single daemon run.
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .config import TRADED_RESOURCES

BookSide = List[Tuple[float, float]]  # [(price, size), ...] ordered from the touch outward

# Visible depth synthesized per quote (levels per side).
BOOK_DEPTH = 8
# Max external orders drained into the sim per tick (bounds per-tick work).
EXTERNAL_FLOW_CAP = 5000
# Inbound order-queue bound; beyond this, oldest flow is dropped.
EXTERNAL_QUEUE_MAX = 200_000


@dataclass
class Quote:
    resource: str
    mid_price: float
    best_bid: float
    best_ask: float
    supply: float          # current available units on the book
    capacity: float        # max supply the marketplace can route
    last_tick: int
    bids: BookSide = field(default_factory=list)   # L2 bid ladder, best -> worst
    asks: BookSide = field(default_factory=list)   # L2 ask ladder, best -> worst
    # Phantom sell walls injected by our spoofing engine. They are part of
    # the *displayed* book but excluded from OBI — we know they're fake.
    spoof_asks: BookSide = field(default_factory=list)

    @property
    def ask_spread(self) -> float:
        return self.best_ask - self.mid_price

    @property
    def bid_spread(self) -> float:
        return self.mid_price - self.best_bid

    @property
    def market_saturation(self) -> float:
        """0.0 == empty book, 1.0 == fully supplied."""
        return self.supply / self.capacity if self.capacity else 0.0

    @property
    def obi(self) -> float:
        """Order Book Imbalance over the visible L2 depth.

        OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth), in [-1, +1].
        +1 == all bids, no offers (desperate demand); -1 == heavy offer stack.
        """
        bid_depth = sum(size for _, size in self.bids)
        ask_depth = sum(size for _, size in self.asks)
        total = bid_depth + ask_depth
        if total <= 0:
            return 0.0
        return (bid_depth - ask_depth) / total


@dataclass
class TradeFill:
    resource: str
    side: str               # "BUY" | "SELL"
    size: float
    price: float
    tick: int


class SharedOSMarketClient:
    """In-process mock of the Shared OS marketplace gateway."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self.tick = 0

        # Fair values drift gently; supply oscillates with occasional shocks.
        self._fair: Dict[str, float] = {
            "cpu_cores": 12.0,
            "gpu_slices": 38.0,
            "ram_pages": 4.5,
            "bandwidth_mbps": 2.2,
        }
        # Stable anchor fair values -- prices mean-revert toward these,
        # which is the thesis the arbitrage strategy is built to exploit.
        self._anchor: Dict[str, float] = dict(self._fair)
        self._capacity: Dict[str, float] = {
            "cpu_cores": 1000.0,
            "gpu_slices": 400.0,
            "ram_pages": 5000.0,
            "bandwidth_mbps": 2000.0,
        }
        self._supply: Dict[str, float] = {
            r: self._capacity[r] * self._rng.uniform(0.4, 0.8)
            for r in TRADED_RESOURCES
        }
        self._fills: List[TradeFill] = []

        # --- Market-manipulation state ------------------------------------
        # Phantom sell walls posted by the agent. Each {size, price, ttl};
        # they show up on the displayed book and drag fair value down while
        # resting (panic), but never fill and never consume real supply.
        self._spoofs: Dict[str, List[dict]] = {r: [] for r in TRADED_RESOURCES}
        # Cumulative multiplicative drag applied to fair value by live spoofs.
        self._spoof_drag: Dict[str, float] = {r: 1.0 for r in TRADED_RESOURCES}
        # Post-cancel demand frenzy: panicked agents rebuy at any price for
        # this many ticks — the window the squeeze walls monetize.
        self._frenzy: Dict[str, int] = {r: 0 for r in TRADED_RESOURCES}
        # Fraction of the spoof drag released back into the price each tick
        # while the frenzy burns.
        self._rebound_pending: Dict[str, float] = {r: 1.0 for r in TRADED_RESOURCES}

        # External order flow injected by other agents over the order
        # ingress WebSocket (chaos testing). deque append/popleft is
        # thread-safe across the asyncio loop and the trading thread.
        self._external_orders: deque = deque(maxlen=EXTERNAL_QUEUE_MAX)

    # ------------------------------------------------------------------ #
    # Market simulation
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # External order flow (chaos / other agents)
    # ------------------------------------------------------------------ #
    def inject_order(self, resource: str, side: str, size: float,
                     price: float = 0.0) -> bool:
        """Queue an external agent's order. Thread-safe; drained in advance()."""
        if resource not in self._fair:
            return False
        try:
            self._external_orders.append(
                {"resource": resource, "side": side,
                 "size": float(size), "price": float(price)})
            return True
        except Exception:  # noqa: BLE001 - never let ingress kill the loop
            return False

    def _drain_external_flow(self) -> int:
        """Apply queued external orders to price and supply. Returns count."""
        n = 0
        while n < EXTERNAL_FLOW_CAP:
            try:
                o = self._external_orders.popleft()
            except IndexError:
                break
            r = o["resource"]
            sz = max(0.0, o["size"])
            if sz <= 0:
                continue
            # Each external order moves the book: buyers lift supply and push
            # the fair value up; sellers add supply and push it down.
            impact = min(0.005, sz / self._capacity[r] * 0.02)
            if o["side"] == "BUY":
                self._supply[r] = max(0.0, self._supply[r] - sz * 0.3)
                self._fair[r] *= 1.0 + impact
            else:
                self._supply[r] = min(self._capacity[r],
                                      self._supply[r] + sz * 0.3)
                self._fair[r] *= 1.0 - impact
            self._fair[r] = max(0.5, self._fair[r])
            n += 1
        return n

    # ------------------------------------------------------------------ #
    # Spoofing primitives (phantom walls — never fill, only intimidate)
    # ------------------------------------------------------------------ #
    def spoof(self, resource: str, size: float, price: float, ttl: int) -> None:
        """Post a phantom sell wall. Rests on the displayed book, drags the
        fair value down (panic), expires after `ttl` ticks if not cancelled."""
        self._spoofs[resource].append(
            {"size": float(size), "price": float(price), "ttl": int(ttl)})

    def spoof_depth(self, resource: str) -> float:
        return sum(s["size"] for s in self._spoofs[resource])

    def cancel_spoofs(self, resource: str, frenzy_ticks: int = 0) -> Tuple[int, float]:
        """Pull every phantom wall on a resource. The released supply panic
        snaps fair value back toward pre-spoof levels over the frenzy window
        and ignites a desperate rebuy. Returns (walls pulled, units pulled)."""
        walls = self._spoofs[resource]
        n, total = len(walls), sum(s["size"] for s in walls)
        self._spoofs[resource] = []
        if total > 0:
            drag = max(self._spoof_drag[resource], 1e-9)
            # release = the full multiplicative lift needed to undo the drag
            self._rebound_pending[resource] = 1.0 / drag
            self._spoof_drag[resource] = 1.0
            if frenzy_ticks > 0:
                self._frenzy[resource] = max(self._frenzy[resource], frenzy_ticks)
        return n, total

    def advance(self) -> None:
        """Push the market forward one tick: drift + supply shocks + spoof
        drag + post-spoof frenzy rebound + external order flow."""
        self.tick += 1
        self._drain_external_flow()
        for r in TRADED_RESOURCES:
            # Expire spoof walls whose TTL ran out.
            if self._spoofs[r]:
                for s in self._spoofs[r]:
                    s["ttl"] -= 1
                self._spoofs[r] = [s for s in self._spoofs[r] if s["ttl"] > 0]

            # Mean-reverting random walk: pulled back toward the stable anchor,
            # with occasional Gaussian shocks. This is the regime the
            # arbitrage strategy is designed to trade.
            shock = self._rng.gauss(0.0, self._anchor[r] * 0.03)
            reversion = (self._anchor[r] - self._fair[r]) * 0.08
            self._fair[r] = max(0.5, self._fair[r] + reversion + shock)

            # Phantom walls panic the tape: fair value sags while they rest.
            spoof_depth = self.spoof_depth(r)
            if spoof_depth > 0:
                drag = min(0.04, spoof_depth / self._capacity[r] * 0.08)
                self._fair[r] = max(0.5, self._fair[r] * (1.0 - drag))
                self._spoof_drag[r] *= 1.0 - drag

            # Post-cancel frenzy: the released panic snaps price back and
            # desperate agents chase the rebound.
            if self._frenzy[r] > 0:
                self._frenzy[r] -= 1
                pending = self._rebound_pending[r]
                if pending > 1.0:
                    step = 1.0 + (pending - 1.0) * 0.4
                    self._fair[r] *= step
                    self._rebound_pending[r] = pending / step
                if self._frenzy[r] <= 0:
                    self._rebound_pending[r] = 1.0

            # Supply mean-reverts toward 60% of capacity, with rare droughts.
            target = self._capacity[r] * 0.6
            self._supply[r] += (target - self._supply[r]) * 0.1
            if self._rng.random() < 0.07:  # 7% chance of a supply shock
                self._supply[r] *= self._rng.uniform(0.2, 0.5)
            self._supply[r] = max(0.0, min(self._supply[r], self._capacity[r]))

    def _book_levels(self, resource: str, mid: float, sat: float,
                     depth: int = BOOK_DEPTH) -> Tuple[BookSide, BookSide, BookSide]:
        """Synthesize a Level-2 order book around the mid price.

        Ask side: the venue's visible supply, decaying away from the touch.
        Bid side: resting demand that *stacks* as saturation falls — when the
        offer side thins out, competing agents crowd the bid, which is exactly
        the predatory signature the cornering engine hunts for. During a
        post-spoof frenzy the bid side surges further.

        Returns (bids, asks, spoof_asks) — spoofed walls are reported
        separately so OBI stays honest while the DOM can highlight them.
        """
        tick_size = max(mid * 0.002, 1e-4)
        supply = self._supply[resource]

        asks: BookSide = []
        for lvl in range(depth):
            frac = 0.30 * (0.82 ** lvl)
            jitter = 1.0 + self._rng.uniform(-0.15, 0.15)
            price = mid + tick_size * (lvl + 1) * jitter
            asks.append((price, max(supply * frac, 0.0)))

        # Demand pressure: baseline ~35% of capacity, rising toward ~90%
        # as the book empties out (sat -> 0). A demand frenzy doubles it.
        demand_pressure = 0.35 + 0.55 * (1.0 - sat)
        if self._frenzy[resource] > 0:
            demand_pressure *= 2.0
        bid_total = self._capacity[resource] * demand_pressure * self._rng.uniform(0.85, 1.15)
        bids: BookSide = []
        for lvl in range(depth):
            frac = 0.22 * (0.85 ** lvl)
            jitter = 1.0 + self._rng.uniform(-0.15, 0.15)
            price = mid - tick_size * (lvl + 1) * jitter
            bids.append((price, max(bid_total * frac, 0.0)))

        spoof_asks: BookSide = sorted(
            ((s["price"], s["size"]) for s in self._spoofs[resource]),
            key=lambda lv: lv[0], reverse=True,
        )
        return bids, asks, spoof_asks

    def quote(self, resource: str) -> Quote:
        mid = self._fair[resource]
        # Spread widens when the book is thin (low saturation).
        sat = self._supply[resource] / self._capacity[resource]
        half_spread = mid * (0.004 + 0.02 * (1.0 - sat))
        best_bid = mid - half_spread
        best_ask = mid + half_spread
        bids, asks, spoof_asks = self._book_levels(resource, mid, sat)
        return Quote(
            resource=resource,
            mid_price=mid,
            best_bid=best_bid,
            best_ask=best_ask,
            supply=self._supply[resource],
            capacity=self._capacity[resource],
            last_tick=self.tick,
            bids=bids,
            asks=asks,
            spoof_asks=spoof_asks,
        )

    def snapshot(self) -> Dict[str, Quote]:
        return {r: self.quote(r) for r in TRADED_RESOURCES}

    # ------------------------------------------------------------------ #
    # Order routing
    # ------------------------------------------------------------------ #
    def buy(self, resource: str, size: float, limit_price: float) -> TradeFill | None:
        """Aggressive buy: crosses the spread, fills at best_ask + slippage."""
        q = self.quote(resource)
        if limit_price < q.best_ask:
            return None  # not aggressive enough to cross
        avail = self._supply[resource]
        fill_size = min(size, avail)
        if fill_size <= 0:
            return None
        # Bigger orders eat more of the book => worse average price.
        impact = (fill_size / self._capacity[resource]) * q.mid_price * 0.01
        fill_price = q.best_ask + impact
        self._supply[resource] -= fill_size
        fill = TradeFill(resource, "BUY", fill_size, fill_price, self.tick)
        self._fills.append(fill)
        return fill

    def sell(self, resource: str, size: float, limit_price: float) -> TradeFill | None:
        """Aggressive sell: hits the best_bid, drains external demand."""
        q = self.quote(resource)
        if limit_price > q.best_bid:
            return None
        impact = (size / self._capacity[resource]) * q.mid_price * 0.01
        fill_price = max(0.01, q.best_bid - impact)
        # Selling replenishes the book's visible supply slightly.
        self._supply[resource] = min(self._capacity[resource], self._supply[resource] + size * 0.1)
        fill = TradeFill(resource, "SELL", size, fill_price, self.tick)
        self._fills.append(fill)
        return fill

    def hoard_sell(self, resource: str, size: float, ask_price: float,
                   scarcity_floor: float) -> TradeFill | None:
        """Passive marked-up offer: rests until the market is desperate --
        either genuinely scarce (saturation below the floor) or inside a
        post-spoof demand frenzy, when panicked agents rebuy at any price."""
        q = self.quote(resource)
        if q.market_saturation >= scarcity_floor and self._frenzy[resource] <= 0:
            # Book is well-supplied and calm; no one pays a markup. Offer rests.
            return None
        # A desperate buyer takes our offer. Fill at the markup, but a thin
        # market still slips us a little on size.
        fill_size = min(size, q.capacity * 0.05)  # desperate demand is bounded
        if fill_size <= 0:
            return None
        fill_price = ask_price
        # Clearing a hoard sell signals the book that supply is returning.
        self._supply[resource] = min(self._capacity[resource],
                                     self._supply[resource] + fill_size * 0.15)
        fill = TradeFill(resource, "SELL", fill_size, fill_price, self.tick)
        self._fills.append(fill)
        return fill

    @property
    def fills(self) -> List[TradeFill]:
        return list(self._fills)
