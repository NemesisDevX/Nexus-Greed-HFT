"""Bus, candles, wire format — shared by server.py and lite_server.py."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Optional, Set

from .config import TRADED_RESOURCES
from .market_client import Quote, TradeFill
from .strategy import BidIntent

CHARTED_RESOURCES = ("cpu_cores", "gpu_slices")
CANDLE_SECONDS = 1
STREAM_INTERVAL = 0.05


# --------------------------------------------------------------------------- #
# candles
# --------------------------------------------------------------------------- #
class CandleAggregator:
    """1s OHLC off the tick stream; forming candle re-emits every tick."""

    def __init__(self, resources: tuple[str, ...] = CHARTED_RESOURCES,
                 bucket_seconds: int = CANDLE_SECONDS) -> None:
        self._resources = resources
        self._bucket_seconds = bucket_seconds
        self._candles: Dict[str, List[Dict[str, Any]]] = {r: [] for r in resources}
        self._current: Dict[str, Optional[Dict[str, Any]]] = {r: None for r in resources}

    def update(self, resource: str, price: float, ts: int) -> Optional[Dict[str, Any]]:
        if resource not in self._candles:
            return None
        bucket = ts - (ts % self._bucket_seconds)
        cur = self._current[resource]
        if cur is None or cur["time"] != bucket:
            cur = {"time": bucket, "open": price, "high": price,
                   "low": price, "close": price}
            self._candles[resource].append(cur)
            self._current[resource] = cur
            if len(self._candles[resource]) > 1200:
                self._candles[resource] = self._candles[resource][-1000:]
        else:
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["close"] = price
        return dict(cur)

    def history(self, resource: str, n: int = 300) -> List[Dict[str, Any]]:
        return [dict(c) for c in self._candles[resource][-n:]]


# --------------------------------------------------------------------------- #
# thread -> loop fanout
# --------------------------------------------------------------------------- #
class MarketEventBus:
    """publish() runs on the trading thread; the broadcaster serializes once
    and hands every sub the same payload. Full queues drop oldest — a laggy
    client eats stale-frame loss, never the producer."""

    def __init__(self, loop: asyncio.AbstractEventLoop, max_queue: int = 256,
                 serialize: Callable[[Dict[str, Any]], str] = json.dumps) -> None:
        self._loop = loop
        self._max_queue = max_queue
        self._serialize = serialize
        self._subscribers: Set[asyncio.Queue] = set()
        self._ingress: asyncio.Queue = asyncio.Queue(maxsize=max_queue)

    def publish(self, event: Dict[str, Any]) -> None:
        try:
            self._loop.call_soon_threadsafe(self._safe_put, event)
        except RuntimeError:
            pass  # loop died mid-shutdown

    def _safe_put(self, event: Dict[str, Any]) -> None:
        try:
            self._ingress.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._ingress.get_nowait()
                self._ingress.put_nowait(event)
            except asyncio.QueueEmpty:
                pass

    async def broadcaster(self) -> None:
        while True:
            event = await self._ingress.get()
            try:
                payload = self._serialize(event)
            except (TypeError, ValueError):
                continue
            for q in list(self._subscribers):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except asyncio.QueueEmpty:
                        pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# --------------------------------------------------------------------------- #
# wire format
# --------------------------------------------------------------------------- #
def _quote_payload(q: Quote) -> Dict[str, Any]:
    return {
        "resource": q.resource,
        "mid_price": round(q.mid_price, 4),
        "best_bid": round(q.best_bid, 4),
        "best_ask": round(q.best_ask, 4),
        "ask_spread": round(q.ask_spread, 6),
        "supply": round(q.supply, 2),
        "capacity": round(q.capacity, 2),
        "market_saturation": round(q.market_saturation, 4),
        "obi": round(q.obi, 4),
        "book": {
            "bids": [[round(p, 4), round(s, 2)] for p, s in q.bids],
            "asks": [[round(p, 4), round(s, 2)] for p, s in q.asks],
            "spoof_asks": [[round(p, 4), round(s, 2)] for p, s in q.spoof_asks],
        },
    }


def _fill_marker(intent: BidIntent, fill: TradeFill, ts: int) -> Dict[str, Any]:
    return {
        "time": ts - (ts % CANDLE_SECONDS),
        "resource": intent.resource,
        "side": intent.side,
        "regime": intent.regime,
        "price": round(fill.price, 4),
        "size": round(fill.size, 4),
        "rationale": intent.rationale,
        "tick": fill.tick,
    }


class StreamFormatter:
    """Runs on the trading thread inside on_tick — no locking needed."""

    def __init__(self) -> None:
        self.candles = CandleAggregator()
        self._markers: Dict[str, List[Dict[str, Any]]] = {r: [] for r in CHARTED_RESOURCES}

    def build(self, snapshot: Dict[str, Quote], ledger: Dict[str, Any],
              fills: List[tuple[BidIntent, TradeFill]]) -> Dict[str, Any]:
        ts = int(time.time())
        live_candles: Dict[str, Dict[str, Any]] = {}
        for r in CHARTED_RESOURCES:
            c = self.candles.update(r, snapshot[r].mid_price, ts)
            if c is not None:
                live_candles[r] = c

        new_markers: List[Dict[str, Any]] = []
        new_fills: List[Dict[str, Any]] = []
        for intent, fill in fills:
            m = _fill_marker(intent, fill, ts)
            new_fills.append(m)
            # SPOOF/CANCEL go to the feed only — they never print on the tape
            if intent.resource not in self._markers or intent.side not in ("BUY", "SELL"):
                continue
            self._markers[intent.resource].append(m)
            if len(self._markers[intent.resource]) > 500:
                self._markers[intent.resource] = self._markers[intent.resource][-400:]
            new_markers.append(m)

        return {
            "ts": ts,
            "tick": ledger["tick"],
            "quotes": {r: _quote_payload(snapshot[r]) for r in TRADED_RESOURCES},
            "candles": live_candles,
            "markers": {r: list(self._markers[r]) for r in CHARTED_RESOURCES},
            "new_markers": new_markers,
            "new_fills": new_fills,
            "ledger": ledger,
        }
