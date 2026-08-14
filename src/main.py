"""C2 entry point.

Supports two modes:
- ``cli``: interactive command loop with multi-Agent switching.
- ``web``: starts FastAPI + NetworkServer together via uvicorn.
"""

import argparse
import asyncio
import sys
import os

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    base_path = sys._MEIPASS
else:
    base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base_path)

from src.c2.agent_registry import AgentRegistry
from src.c2.forwarder import NetworkError
from src.c2.network_server import NetworkServer
from src.config import Config
from src.controller import Controller
from src.model import ModelModule


def print_help():
    print("\nAvailable commands:")
    print("  /quit        - Exit the program")
    print("  /help        - Show this help message")
    print("  /reset       - Reset conversation history for the active Agent")
    print("  /agents      - List connected Agents")
    print("  /target <id> - Switch active Agent (and its LLM session)")
    print("  /y-all       - Auto-authorize subsequent commands")
    print("  /n-all       - Require authorization for subsequent commands")
    print("  /upload <local> <dest>  - Upload local file to active Agent")
    print("  /download <src>         - Download file from active Agent to program dir")
    print("  /shutdown               - Shut down the active Agent (disconnect + exit)")
    print("\nDuring authorization prompts:")
    print("  /y      - Allow the current command")
    print("  /n      - Deny the current command")
    print()


async def cli_main(config_path: str):
    cfg = Config(config_path)
    registry = AgentRegistry()
    controller = Controller(cfg, registry)
    server = NetworkServer(
        registry=registry,
        host=cfg.c2_host,
        port=cfg.c2_port,
        auth_token=cfg.c2_auth_tokens,
        heartbeat_timeout=cfg.heartbeat_timeout_sec,
    )
    model = ModelModule(controller)

    server_task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)

    print("=" * 60)
    print("AI Remote Control Tool - C2 (CLI)")
    print("=" * 60)
    print(f"OS: {controller.system_name}")
    print(f"Model: {cfg.model}")
    print(f"API Base: {cfg.api_base}")
    print(f"C2 Network: {cfg.c2_host}:{server.port}")
    print(f"Auth Mode: {'Auto-authorized' if controller.get_auth_mode() == 1 else 'Authorization required'}")
    print("=" * 60)
    print("\nCommands:")
    print_help()
    print("Multi-turn conversation enabled. Max iterations:", cfg.round_limit)
    print()

    loop = asyncio.get_event_loop()

    def _read_input():
        return input("\nYou: ")

    while True:
        try:
            user_input = await loop.run_in_executor(None, _read_input)
            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input.lower() in ("/quit", "/q"):
                print("Goodbye!")
                break

            if user_input.lower() == "/help":
                print_help()
                continue

            if user_input.lower() == "/reset":
                model.reset_conversation()
                print("Conversation reset for active Agent.")
                continue

            if user_input.lower() == "/agents":
                agents = registry.list_all()
                if not agents:
                    print("No agents connected.")
                else:
                    for a in agents:
                        marker = "*" if a.id == registry.active_id else " "
                        print(f" {marker} {a.id:<20} {a.os:<10} {a.hostname}")
                    print(f"\n(* = active)")
                continue

            if user_input.lower().startswith("/target "):
                target = user_input.split(maxsplit=1)[1].strip()
                if registry.set_active(target):
                    print(f"Active agent -> {target}")
                    print(f"  OS: {controller.system_name}")
                else:
                    print(f"No such agent: {target}")
                continue

            if user_input.lower() == "/n-all":
                controller.set_auth_mode(0)
                print("Authorization required for all commands.")
                continue

            if user_input.lower() == "/y-all":
                controller.set_auth_mode(1)
                print("Auto-authorization enabled for all commands.")
                continue

            if user_input.lower().startswith("/upload "):
                from src.c2 import file_transfer
                parts = user_input.split(maxsplit=2)
                if len(parts) < 3:
                    print("Usage: /upload <local_path> <dest_path>")
                    continue
                try:
                    result = await file_transfer.upload(registry, parts[1], parts[2])
                    print(result)
                except NetworkError as e:
                    print(f"Error: {e}")
                continue

            if user_input.lower().startswith("/download "):
                from src.c2 import file_transfer
                parts = user_input.split(maxsplit=1)
                if len(parts) < 2:
                    print("Usage: /download <src_path>")
                    continue
                try:
                    result = await file_transfer.download(
                        registry, parts[1], download_dir=cfg.get_dl_dir()
                    )
                    print(f"Saved to: {result}")
                except NetworkError as e:
                    print(f"Error: {e}")
                continue

            if user_input.lower() == "/shutdown":
                agent = registry.get_active()
                if agent is None:
                    print("No active agent.")
                    continue
                from common.protocol import CMD_SHUTDOWN
                try:
                    await controller.forwarder.send_control(CMD_SHUTDOWN, ["shutdown"])
                    print(f"Shutdown command sent to {agent.id}.")
                except NetworkError as e:
                    print(f"Error: {e}")
                continue

            result = await model.chat_async(user_input, stream_to_stdout=True)
            if result.error:
                print(f"\nError: {result.error}")
        except KeyboardInterrupt:
            print("\nInterrupted. Type /quit to exit.")
        except Exception as e:
            print(f"\nError: {str(e)}")

    server_task.cancel()
    try:
        await server_task
    except (asyncio.CancelledError, Exception):
        pass


def main():
    parser = argparse.ArgumentParser(description="AI Remote Control Tool - C2")
    parser.add_argument("--mode", choices=["cli", "web"], default="cli")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    if args.mode == "web":
        from src.web_server import main as web_main
        web_main(args.config)
    else:
        try:
            asyncio.run(cli_main(args.config))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()