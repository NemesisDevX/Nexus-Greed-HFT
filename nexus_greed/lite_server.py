"""Pure-websockets streamer — same wire protocol as server.py, no compiled
deps. WS /ws (stream + order ingress), GET /, /api/status, /demo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .agent import NexusGreedAgent
from .config import TRADED_RESOURCES, StrategyConfig
from .market_client import Quote, TradeFill
from .strategy import BidIntent
from .streaming import STREAM_INTERVAL, MarketEventBus, StreamFormatter

log = logging.getLogger("nexus_greed.lite")

DASHBOARD = Path(__file__).parent / "static" / "dashboard.html"

_JSON = {"Content-Type": "application/json",
         "Access-Control-Allow-Origin": "*"}
_HTML = {"Content-Type": "text/html; charset=utf-8",
         "Access-Control-Allow-Origin": "*"}


async def _ws_handler(ws: Any, bus: MarketEventBus, order_sink: Any) -> None:
    q = bus.subscribe()
    stop = asyncio.Event()
    last_payload: Optional[str] = None

    async def order_reader() -> None:
        if order_sink is None:
            return
        try:
            async for raw in ws:
                if stop.is_set():
                    break
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(msg, dict) and msg.get("type") == "order":
                    try:
                        order_sink(str(msg.get("resource", "")),
                                   str(msg.get("side", "")),
                                   float(msg.get("size", 0) or 0),
                                   float(msg.get("price", 0) or 0))
                    except (TypeError, ValueError):
                        continue
        except Exception:  # noqa: BLE001 — client vanished
            pass
        finally:
            stop.set()

    reader = asyncio.create_task(order_reader())
    try:
        while not stop.is_set():
            payload = last_payload
            while not q.empty():
                payload = q.get_nowait()  # freshest wins
            if payload is not None:
                await ws.send(payload)
                last_payload = payload
            # scale the send rate with fanout or one loop drowns at 10k conns
            await asyncio.sleep(
                max(STREAM_INTERVAL, bus.subscriber_count * 1e-4))
    except Exception:  # noqa: BLE001 — drops are routine under churn
        pass
    finally:
        stop.set()
        reader.cancel()
        bus.unsubscribe(q)


def _respond(connection: Any, status: int, text: str,
             headers: Dict[str, str]):
    # websockets>=17: respond() has no headers kwarg — mutate after
    resp = connection.respond(status, text)
    for k, v in headers.items():
        resp.headers[k] = v
    return resp


def _make_process_request(get_state: Any, dash_html: bytes):
    def process_request(connection: Any, request: Any):
        path = request.path.split("?")[0]
        if path == "/ws":
            return None  # proceed with the WebSocket handshake
        if path == "/":
            return _respond(
                connection, 200,
                json.dumps({"status": "live", "agent": "nexus-greed",
                            "server": "lite",
                            "resources": list(TRADED_RESOURCES)}), _JSON)
        if path == "/api/status":
            return _respond(connection, 200, json.dumps(get_state()), _JSON)
        if path == "/demo":
            return _respond(connection, 200, dash_html.decode("utf-8"), _HTML)
        return _respond(connection, 404, "not found", _JSON)
    return process_request


def run_lite_server(cfg: StrategyConfig, host: str = "127.0.0.1",
                    port: int = 8000, seed: Optional[int] = None) -> None:
    from websockets.asyncio.server import serve

    try:
        import uvloop
        uvloop.install()
        loop_kind = "uvloop"
    except ImportError:
        loop_kind = "asyncio (stock)"

    dash_html = DASHBOARD.read_bytes() if DASHBOARD.exists() else b"<h1>nexus-greed</h1>"

    async def main() -> None:
        loop = asyncio.get_running_loop()
        # ~4KB frames x 10k conns: deep queues would OOM us
        bus = MarketEventBus(loop, max_queue=8)
        formatter = StreamFormatter()
        latest: Dict[str, Any] = {"state": {}}
        loop_ref = loop
        pub_skip = [0]

        def on_tick(snapshot: Dict[str, Quote], ledger: Dict[str, Any],
                    fills: list) -> None:
            event = formatter.build(snapshot, ledger, fills)
            pub_skip[0] += 1
            # past ~2k subs, per-conn cadence is already >=0.2s — broadcasting
            # every tick just burns loop CPU on frames nobody can consume
            if bus.subscriber_count <= 2000 or pub_skip[0] % 3 == 0:
                bus.publish(event)

            def _stash() -> None:
                latest["state"] = event
            try:
                loop_ref.call_soon_threadsafe(_stash)
            except RuntimeError:
                pass

        agent_cfg = cfg
        if cfg.tick_seconds >= 0.5:
            agent_cfg = StrategyConfig(**{**cfg.__dict__, "tick_seconds": 0.15})
        agent = NexusGreedAgent(cfg=agent_cfg, seed=seed, on_tick=on_tick)

        def get_state() -> Dict[str, Any]:
            return latest["state"] or {"status": "warming up"}

        async def handler(ws: Any) -> None:
            await _ws_handler(ws, bus, agent.market.inject_order)

        broadcaster_task = asyncio.create_task(bus.broadcaster())
        trading_thread = threading.Thread(target=agent.run, daemon=True,
                                          name="nexus-trading")
        trading_thread.start()

        # no permessage-deflate — it eats the loop alive at scale
        async with serve(handler, host, port,
                         process_request=_make_process_request(get_state, dash_html),
                         compression=None, max_queue=32,
                         ping_interval=None,
                         open_timeout=60, backlog=1024):
            log.info("lite server live on http://%s:%d (loop=%s, subs ready)",
                     host, port, loop_kind)
            try:
                await asyncio.Future()  # serve forever
            finally:
                broadcaster_task.cancel()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("interrupted")
