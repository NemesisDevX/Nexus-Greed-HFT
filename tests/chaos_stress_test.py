"""Chaos harness: N agents on /ws, order flood inbound, churn kill-waves.

  python tests/chaos_stress_test.py --agents 10000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from typing import Set

try:
    from websockets.asyncio.client import connect
except ImportError:  # websockets < 15
    from websockets import connect  # type: ignore

try:
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover
    ConnectionClosed = OSError  # type: ignore

RESOURCES = ("cpu_cores", "gpu_slices", "ram_pages", "bandwidth_mbps")

STOP = asyncio.Event()
live: Set = set()

stats = {
    "open_ok": 0,
    "msgs_in": 0,
    "orders_out": 0,
    "reconnects": 0,
    "killed": 0,
    "dropped": 0,   # churn casualties — routine, not errors
    "errors": 0,    # anything else — actually interesting
}


async def agent_session(target: str, order_rate: float, order_frac: float) -> None:
    rng = random.Random()
    sends_orders = rng.random() < order_frac
    while not STOP.is_set():
        try:
            async with connect(target, open_timeout=30,
                               ping_interval=None, max_queue=64) as ws:
                live.add(ws)
                stats["open_ok"] += 1
                if stats["open_ok"] > 1:
                    stats["reconnects"] += 1
                try:
                    while not STOP.is_set():
                        if sends_orders:
                            order = {
                                "type": "order",
                                "resource": rng.choice(RESOURCES),
                                "side": rng.choice(("BUY", "SELL")),
                                "size": round(rng.uniform(1, 200), 2),
                                "price": round(rng.uniform(1, 50), 3),
                            }
                            await ws.send(json.dumps(order))
                            stats["orders_out"] += 1
                        try:
                            msg = await asyncio.wait_for(
                                ws.recv(), timeout=order_rate)
                            stats["msgs_in"] += 1
                        except asyncio.TimeoutError:
                            continue
                finally:
                    live.discard(ws)
        except ConnectionClosed:
            stats["dropped"] += 1
        except (OSError, asyncio.IncompleteReadError, EOFError,
                asyncio.TimeoutError, TimeoutError):
            stats["dropped"] += 1
        except Exception:  # noqa: BLE001 — harness must never die
            stats["errors"] += 1
        if not STOP.is_set():
            await asyncio.sleep(rng.uniform(0.05, 0.4))


async def churn(frac: float, interval: float) -> None:
    # transport.abort() = instant RST. awaiting close() under load lags the
    # kill wave by seconds
    rng = random.Random()
    while not STOP.is_set():
        await asyncio.sleep(interval)
        victims = list(live)
        rng.shuffle(victims)
        for ws in victims[: int(len(victims) * frac)]:
            stats["killed"] += 1
            try:
                ws.transport.abort()
            except Exception:  # noqa: BLE001
                try:
                    ws.close()  # fire-and-forget
                except Exception:  # noqa: BLE001
                    pass


async def stats_loop(agents: int) -> None:
    t0 = time.monotonic()
    last_msgs = last_orders = 0
    while not STOP.is_set():
        await asyncio.sleep(5.0)
        dt = time.monotonic() - t0
        msgs, orders = stats["msgs_in"], stats["orders_out"]
        print(
            f"[{dt:7.1f}s] alive={len(live):>6}/{agents}  "
            f"stream={msgs - last_msgs:>9,}/5s  orders={orders - last_orders:>8,}/5s  "
            f"reconn={stats['reconnects']:>7,}  killed={stats['killed']:>7,}  "
            f"drops={stats['dropped']:>6,}  err={stats['errors']}",
            flush=True,
        )
        last_msgs, last_orders = msgs, orders


async def main_async(args: argparse.Namespace) -> None:
    target = f"ws://{args.host}:{args.port}/ws"
    tasks = [asyncio.create_task(stats_loop(args.agents)),
             asyncio.create_task(churn(args.churn_fraction, args.churn_interval))]

    # batched ramp — an unthrottled 10k connect storm just SYN-floods us
    batch = 400
    for i in range(0, args.agents, batch):
        for j in range(i, min(i + batch, args.agents)):
            tasks.append(asyncio.create_task(
                agent_session(target, args.order_interval, args.order_fraction)))
        await asyncio.sleep(0.05)
        if i and i % (batch * 5) == 0:
            print(f"ramped {i:,}/{args.agents:,} connections (alive={len(live)})",
                  flush=True)

    print(f"ramped {args.agents:,} agent connections", flush=True)
    if args.duration > 0:
        try:
            await asyncio.wait_for(STOP.wait(), timeout=args.duration)
        except asyncio.TimeoutError:
            pass
        STOP.set()
    else:
        await STOP.wait()

    for ws in list(live):
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    for t in tasks:
        t.cancel()
    print("chaos harness stopped", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Nexus-Greed chaos stress test")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--agents", type=int, default=10_000,
                   help="synthetic agent connections (default 10,000)")
    p.add_argument("--order-interval", type=float, default=1.5,
                   help="seconds between order sends per agent")
    p.add_argument("--order-fraction", type=float, default=0.35,
                   help="fraction of agents that also send order flow")
    p.add_argument("--churn-fraction", type=float, default=0.10,
                   help="fraction of live conns killed each churn cycle")
    p.add_argument("--churn-interval", type=float, default=2.0,
                   help="seconds between kill waves (default 2s)")
    p.add_argument("--duration", type=float, default=0.0,
                   help="stop after N seconds (0 = run until Ctrl-C)")
    args = p.parse_args()

    print(f"CHAOS: {args.agents:,} agents -> ws://{args.host}:{args.port}/ws "
          f"(churn {args.churn_fraction:.0%} every {args.churn_interval}s)", flush=True)
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
