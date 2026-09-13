"""In-process venue sim — mean-reverting fair value, L2 book, spoof mechanics.

Stateful on purpose: the agent must see one consistent market per run.
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .config import TRADED_RESOURCES

BookSide = List[Tuple[float, float]]  # [(price, size), ...] ordered from the touch outward

BOOK_DEPTH = 8
EXTERNAL_FLOW_CAP = 5000      # bound per-tick drain work
EXTERNAL_QUEUE_MAX = 200_000  # beyond this, oldest flow dies


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
    # our phantom walls: on the tape, out of OBI — we know they're fake
    spoof_asks: BookSide = field(default_factory=list)

    @property
    def ask_spread(self) -> float:
        return self.best_ask - self.mid_price

    @property
    def bid_spread(self) -> float:
        return self.mid_price - self.best_bid

    @property
    def market_saturation(self) -> float:
        return self.supply / self.capacity if self.capacity else 0.0

    @property
    def obi(self) -> float:
        # (bid-ask)/(bid+ask); +1 = desperate demand, -1 = offer stack
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
    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self.tick = 0

        self._fair: Dict[str, float] = {
            "cpu_cores": 12.0,
            "gpu_slices": 38.0,
            "ram_pages": 4.5,
            "bandwidth_mbps": 2.2,
        }
        # arb edge lives off mean reversion to this anchor
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

        # phantom walls {size,price,ttl}: shown on tape, never fill
        self._spoofs: Dict[str, List[dict]] = {r: [] for r in TRADED_RESOURCES}
        self._spoof_drag: Dict[str, float] = {r: 1.0 for r in TRADED_RESOURCES}
        self._frenzy: Dict[str, int] = {r: 0 for r in TRADED_RESOURCES}
        self._rebound_pending: Dict[str, float] = {r: 1.0 for r in TRADED_RESOURCES}

        # deque append/popleft survives the loop<->thread split
        self._external_orders: deque = deque(maxlen=EXTERNAL_QUEUE_MAX)

    # ------------------------------------------------------------------ #
    # external flow
    # ------------------------------------------------------------------ #
    def inject_order(self, resource: str, side: str, size: float,
                     price: float = 0.0) -> bool:
        """Queue foreign order flow; drained in advance()."""
        if resource not in self._fair:
            return False
        try:
            self._external_orders.append(
                {"resource": resource, "side": side,
                 "size": float(size), "price": float(price)})
            return True
        except Exception:  # noqa: BLE001 — ingress must never kill the loop
            return False

    def _drain_external_flow(self) -> int:
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
            # buys eat supply / lift fair value; sells do the opposite
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
    # spoofing — walls never fill, they only scare
    # ------------------------------------------------------------------ #
    def spoof(self, resource: str, size: float, price: float, ttl: int) -> None:
        self._spoofs[resource].append(
            {"size": float(size), "price": float(price), "ttl": int(ttl)})

    def spoof_depth(self, resource: str) -> float:
        return sum(s["size"] for s in self._spoofs[resource])

    def cancel_spoofs(self, resource: str, frenzy_ticks: int = 0) -> Tuple[int, float]:
        # pull the walls; released panic rebounds over the frenzy window
        walls = self._spoofs[resource]
        n, total = len(walls), sum(s["size"] for s in walls)
        self._spoofs[resource] = []
        if total > 0:
            drag = max(self._spoof_drag[resource], 1e-9)
            self._rebound_pending[resource] = 1.0 / drag
            self._spoof_drag[resource] = 1.0
            if frenzy_ticks > 0:
                self._frenzy[resource] = max(self._frenzy[resource], frenzy_ticks)
        return n, total

    def advance(self) -> None:
        self.tick += 1
        self._drain_external_flow()
        for r in TRADED_RESOURCES:
            if self._spoofs[r]:
                for s in self._spoofs[r]:
                    s["ttl"] -= 1
                self._spoofs[r] = [s for s in self._spoofs[r] if s["ttl"] > 0]

            shock = self._rng.gauss(0.0, self._anchor[r] * 0.03)
            reversion = (self._anchor[r] - self._fair[r]) * 0.08
            self._fair[r] = max(0.5, self._fair[r] + reversion + shock)

            # walls resting on the tape drag fair value
            spoof_depth = self.spoof_depth(r)
            if spoof_depth > 0:
                drag = min(0.04, spoof_depth / self._capacity[r] * 0.08)
                self._fair[r] = max(0.5, self._fair[r] * (1.0 - drag))
                self._spoof_drag[r] *= 1.0 - drag

            if self._frenzy[r] > 0:
                self._frenzy[r] -= 1
                pending = self._rebound_pending[r]
                if pending > 1.0:
                    step = 1.0 + (pending - 1.0) * 0.4
                    self._fair[r] *= step
                    self._rebound_pending[r] = pending / step
                if self._frenzy[r] <= 0:
                    self._rebound_pending[r] = 1.0

            target = self._capacity[r] * 0.6
            self._supply[r] += (target - self._supply[r]) * 0.1
            if self._rng.random() < 0.07:  # 7% chance of a supply shock
                self._supply[r] *= self._rng.uniform(0.2, 0.5)
            self._supply[r] = max(0.0, min(self._supply[r], self._capacity[r]))

    def _book_levels(self, resource: str, mid: float, sat: float,
                     depth: int = BOOK_DEPTH) -> Tuple[BookSide, BookSide, BookSide]:
        # asks: venue supply decaying off the touch. bids: demand *stacks*
        # as the book thins — the signature the cornering engine hunts.
        tick_size = max(mid * 0.002, 1e-4)
        supply = self._supply[resource]

        asks: BookSide = []
        for lvl in range(depth):
            frac = 0.30 * (0.82 ** lvl)
            jitter = 1.0 + self._rng.uniform(-0.15, 0.15)
            price = mid + tick_size * (lvl + 1) * jitter
            asks.append((price, max(supply * frac, 0.0)))

        # bid crowding: 35% of capacity baseline -> ~90% on an empty book
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
        # thin book => wide spread
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
    # routing
    # ------------------------------------------------------------------ #
    def buy(self, resource: str, size: float, limit_price: float) -> TradeFill | None:
        q = self.quote(resource)
        if limit_price < q.best_ask:
            return None  # didn't cross
        avail = self._supply[resource]
        fill_size = min(size, avail)
        if fill_size <= 0:
            return None
        impact = (fill_size / self._capacity[resource]) * q.mid_price * 0.01
        fill_price = q.best_ask + impact
        self._supply[resource] -= fill_size
        fill = TradeFill(resource, "BUY", fill_size, fill_price, self.tick)
        self._fills.append(fill)
        return fill

    def sell(self, resource: str, size: float, limit_price: float) -> TradeFill | None:
        q = self.quote(resource)
        if limit_price > q.best_bid:
            return None
        impact = (size / self._capacity[resource]) * q.mid_price * 0.01
        fill_price = max(0.01, q.best_bid - impact)
        self._supply[resource] = min(self._capacity[resource], self._supply[resource] + size * 0.1)
        fill = TradeFill(resource, "SELL", size, fill_price, self.tick)
        self._fills.append(fill)
        return fill

    def hoard_sell(self, resource: str, size: float, ask_price: float,
                   scarcity_floor: float) -> TradeFill | None:
        # rests until the book is starved OR the frenzy is burning
        q = self.quote(resource)
        if q.market_saturation >= scarcity_floor and self._frenzy[resource] <= 0:
            return None
        fill_size = min(size, q.capacity * 0.05)
        if fill_size <= 0:
            return None
        fill_price = ask_price
        self._supply[resource] = min(self._capacity[resource],
                                     self._supply[resource] + fill_size * 0.15)
        fill = TradeFill(resource, "SELL", fill_size, fill_price, self.tick)
        self._fills.append(fill)
        return fill

    @property
    def fills(self) -> List[TradeFill]:
        return list(self._fills)
