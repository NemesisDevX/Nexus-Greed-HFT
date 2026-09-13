"""python -m nexus_greed {trade|serve|lite}"""
from __future__ import annotations

import argparse
import logging
import sys

from .agent import NexusGreedAgent
from .config import StrategyConfig


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d  %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def _strategy_config_from_args(args: argparse.Namespace) -> StrategyConfig:
    base = StrategyConfig.from_env()
    overrides = {f: getattr(args, f) for f in StrategyConfig.__dataclass_fields__
                 if hasattr(args, f) and getattr(args, f) is not None}
    return StrategyConfig(**{**base.__dict__, **overrides})


def add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--seed", type=int, default=None, help="deterministic market seed")
    p.add_argument("--ticks", type=int, default=0, dest="max_ticks",
                   help="stop after N ticks (0=run forever)")
    p.add_argument("--tick-seconds", type=float, default=1.5, help="seconds between ticks")
    p.add_argument("--starting-cash", type=float, default=10_000.0, help="initial cash")
    p.add_argument("--epsilon", type=float, default=0.10, help="initial exploration rate")
    p.add_argument("--hoard-markup", type=float, default=3.0,
                   help="scarcity sell markup (3.0 = 300 percent)")
    p.add_argument("--verbose", action="store_true", help="DEBUG logging")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nexus_greed",
        description="Nexus-Greed: aggressive computational resource trading agent "
                    "for the Shared OS A2A economy.",
    )
    sub = p.add_subparsers(dest="command")

    trade = sub.add_parser("trade", help="run the headless trading daemon")
    add_common_args(trade)

    serve = sub.add_parser("serve", help="run the FastAPI + WebSocket command center")
    add_common_args(serve)
    serve.add_argument("--host", default="127.0.0.1", help="bind host")
    serve.add_argument("--port", type=int, default=8000, help="bind port")

    lite = sub.add_parser("lite", help="run the pure-websockets fallback server "
                                       "(no FastAPI/pydantic required)")
    add_common_args(lite)
    lite.add_argument("--host", default="127.0.0.1", help="bind host")
    lite.add_argument("--port", type=int, default=8000, help="bind port")

    # keep old flag style working
    add_common_args(p)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    cfg = _strategy_config_from_args(args)

    if args.command == "serve":
        from .server import run_server
        run_server(cfg, host=args.host, port=args.port, seed=args.seed)
        return 0

    if args.command == "lite":
        from .lite_server import run_lite_server
        run_lite_server(cfg, host=args.host, port=args.port, seed=args.seed)
        return 0

    agent = NexusGreedAgent(cfg=cfg, seed=args.seed)
    agent.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
