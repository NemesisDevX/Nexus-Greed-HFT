"""FastAPI streamer. Trading daemon on a thread, uvicorn on the loop,
one call_soon_threadsafe bridge between them.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .agent import NexusGreedAgent
from .config import TRADED_RESOURCES, StrategyConfig
from .market_client import Quote, TradeFill
from .strategy import BidIntent

log = logging.getLogger("nexus_greed.server")

from .streaming import (  # noqa: E402
    CANDLE_SECONDS, CHARTED_RESOURCES, STREAM_INTERVAL,
    MarketEventBus, StreamFormatter,
)


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #
def create_app(bus: MarketEventBus, formatter: StreamFormatter,
               get_state: Any, order_sink: Any = None) -> FastAPI:
    app = FastAPI(title="Nexus-Greed Command Center", version="0.3.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    def root() -> Dict[str, Any]:
        return {"status": "live", "agent": "nexus-greed", "resources": list(TRADED_RESOURCES)}

    @app.get("/api/status")
    def api_status() -> Dict[str, Any]:
        return get_state()

    @app.get("/demo")
    def demo_dashboard() -> Any:
        # zero-dep dashboard — works without the node toolchain
        from fastapi.responses import FileResponse
        html = Path(__file__).parent / "static" / "dashboard.html"
        return FileResponse(html, media_type="text/html")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        q = bus.subscribe()
        last_payload: Optional[str] = None
        stop = asyncio.Event()

        async def order_reader() -> None:
            # same socket, inbound: foreign flow drains into the sim
            if order_sink is None:
                return
            try:
                while not stop.is_set():
                    msg = await ws.receive_json()
                    if isinstance(msg, dict) and msg.get("type") == "order":
                        order_sink(str(msg.get("resource", "")),
                                   str(msg.get("side", "")),
                                   float(msg.get("size", 0) or 0),
                                   float(msg.get("price", 0) or 0))
            except WebSocketDisconnect:
                pass
            except Exception:  # noqa: BLE001 — garbage frames don't kill us
                pass
            finally:
                stop.set()

        reader = asyncio.create_task(order_reader())
        try:
            while not stop.is_set():
                # freshest frame wins
                payload = last_payload
                while not q.empty():
                    payload = q.get_nowait()
                if payload is not None:
                    await ws.send_text(payload)
                    last_payload = payload
                await asyncio.sleep(STREAM_INTERVAL)
        except WebSocketDisconnect:
            log.info("websocket client disconnected")
        except Exception as exc:  # noqa: BLE001
            log.warning("websocket error: %s", exc)
        finally:
            stop.set()
            reader.cancel()
            bus.unsubscribe(q)

    return app


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run_server(cfg: StrategyConfig, host: str = "127.0.0.1",
               port: int = 8000, seed: Optional[int] = None) -> None:
    import uvicorn

    # uvloop on unix; must install before asyncio.run or it does nothing
    try:
        import uvloop
        uvloop.install()
        loop_kind = "uvloop"
    except ImportError:
        loop_kind = "asyncio (stock)"

    async def main() -> None:
        loop = asyncio.get_running_loop()
        bus = MarketEventBus(loop)
        formatter = StreamFormatter()

        latest: Dict[str, Any] = {"state": {}}
        loop_ref = loop

        def on_tick(snapshot: Dict[str, Quote], ledger: Dict[str, Any],
                    fills: List[tuple[BidIntent, TradeFill]]) -> None:
            event = formatter.build(snapshot, ledger, fills)
            bus.publish(event)
            def _stash():
                latest["state"] = event
            try:
                loop_ref.call_soon_threadsafe(_stash)
            except RuntimeError:
                pass

        agent_cfg = cfg
        # nobody wants a 1.5s tape on the dashboard
        if cfg.tick_seconds >= 0.5:
            agent_cfg = StrategyConfig(
                **{**cfg.__dict__, "tick_seconds": 0.15}
            )

        agent = NexusGreedAgent(cfg=agent_cfg, seed=seed, on_tick=on_tick)

        def get_state() -> Dict[str, Any]:
            return latest["state"] or {"status": "warming up"}

        app = create_app(bus, formatter, get_state,
                         order_sink=agent.market.inject_order)
        broadcaster_task = asyncio.create_task(bus.broadcaster())

        trading_thread = threading.Thread(target=agent.run, daemon=True,
                                          name="nexus-trading")
        trading_thread.start()
        log.info("trading thread started; server on http://%s:%d (loop=%s)",
                 host, port, loop_kind)

        config = uvicorn.Config(app, host=host, port=port,
                                log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        try:
            await server.serve()
        finally:
            broadcaster_task.cancel()
            log.info("server shut down")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("interrupted")
