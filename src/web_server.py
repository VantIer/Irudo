"""C2 Web server (FastAPI) with NetworkServer coexisting on the same loop.

Endpoints:

- ``/api/config``              - return model / API base / auth_mode
- ``/api/agents``              - list connected Agents
- ``/api/agents/switch``       - switch active Agent
- ``/api/auth``                - get / set auth_mode
- ``/api/reset``               - reset conversation for active Agent
- ``/api/history``             - get conversation history for active Agent
- ``/api/chat-stream``         - SSE chat stream
- ``/api/exec-cmd``            - direct command execution (active Agent)
- ``/api/files/list``          - list dir on active Agent
- ``/api/files/cwd``           - get/set cwd
- ``/api/files/parent``        - go to parent dir
- ``/api/files/chdir``         - change dir
- ``/api/files/new``           - new file
- ``/api/files/delete``        - delete file
- ``/api/files/download``      - download from active Agent
- ``/api/files/upload``        - upload to active Agent
- ``/api/files/mkdir``         - make directory
- ``/api/files/copy``          - copy
- ``/api/files/move``          - move
"""

import argparse
import asyncio
import json
import os
import posixpath
import re
import sys
import tempfile
from pathlib import Path

if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    base_path = sys._MEIPASS
else:
    base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, base_path)

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import uvicorn

from src.c2 import file_transfer as c2_file_transfer
from src.c2.agent_registry import AgentRegistry
from src.c2.forwarder import NetworkError
from src.c2.network_server import NetworkServer
from src.command import action_to_cmd
from src.config import Config
from src.controller import Controller
from src.model import ModelModule


_LISTING_RE = re.compile(r"^(DIR|FILE)\s+(\d+)\s+(.+)$")


def _parse_listing(text: str) -> list:
    """Parse the remote Agent's list_dir text output into structured items."""
    items = []
    for line in text.splitlines():
        line = line.strip()
        m = _LISTING_RE.match(line)
        if m:
            items.append({
                "name": m.group(3),
                "is_dir": m.group(1) == "DIR",
                "size": int(m.group(2)),
            })
    return items


class WebApp:
    def __init__(self, config_path: str):
        self._config_path = config_path
        self._cfg = Config(config_path)
        self._registry = AgentRegistry()
        self._controller = Controller(self._cfg, self._registry)
        self._model = ModelModule(self._controller, mode="web")
        self._network_server = NetworkServer(
            registry=self._registry,
            host=self._cfg.c2_host,
            port=self._cfg.c2_port,
            auth_tokens=self._cfg.c2_auth_tokens,
            heartbeat_timeout=self._cfg.heartbeat_timeout_sec,
        )

        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            self._web_dir = Path(sys._MEIPASS) / "web"
        else:
            self._web_dir = Path(__file__).parent.parent / "web"

        self._app = FastAPI()
        self._cwd = None  # resolved lazily via remote get_cwd
        self._register_routes()
        self._register_lifespan()

    def _register_lifespan(self):
        @self._app.on_event("startup")
        async def _startup():
            await self._network_server.start()

        @self._app.on_event("shutdown")
        async def _shutdown():
            await self._network_server.stop()

    def _register_routes(self):
        cfg = self._cfg

        @self._app.get("/")
        async def home():
            html_file = self._web_dir / "index.html"
            return FileResponse(str(html_file))

        @self._app.get("/api/config")
        async def get_config():
            return {
                "api_base": cfg.api_base,
                "model": cfg.model,
                "auth_mode": self._controller.get_auth_mode(),
                "active_agent": self._registry.active_id,
            }

        @self._app.get("/api/agents")
        async def list_agents():
            agents = []
            for a in self._registry.list_all():
                d = a.to_dict()
                d["active"] = (a.id == self._registry.active_id)
                agents.append(d)
            return {"agents": agents, "active": self._registry.active_id}

        @self._app.post("/api/agents/switch")
        async def switch_agent(agent_id: str = Form(...)):
            if self._registry.set_active(agent_id):
                self._cwd = None
                return {"success": True, "active": agent_id}
            return JSONResponse({"success": False, "error": "agent not found"}, status_code=404)

        @self._app.post("/api/set-auth")
        async def set_auth(mode: str = Form(...)):
            try:
                m = int(mode)
                self._controller.set_auth_mode(m)
                return {"success": True, "auth_mode": m}
            except ValueError:
                return JSONResponse({"success": False, "error": "invalid mode"}, status_code=400)

        @self._app.post("/api/reset")
        async def reset():
            self._model.reset_conversation()
            return {"success": True}

        @self._app.post("/api/shutdown")
        async def shutdown():
            agent = self._registry.get_active()
            if agent is None:
                return JSONResponse({"success": False, "error": "No active agent"}, status_code=503)
            try:
                from common.protocol import CMD_SHUTDOWN
                await self._controller.forwarder.send_control(CMD_SHUTDOWN, ["shutdown"])
                return {"success": True, "agent": agent.id}
            except NetworkError as e:
                return JSONResponse({"success": False, "error": str(e)}, status_code=503)

        @self._app.get("/api/history")
        async def history():
            return {"history": self._model.get_history()}

        @self._app.post("/api/chat-stream")
        async def chat_stream(message: str = Form(...)):
            async def event_generator():
                async for event in self._model.chat_stream(message):
                    yield f"data: {json.dumps(event)}\n\n"
            return StreamingResponse(event_generator(), media_type="text/event-stream")

        @self._app.post("/api/authorize-execute")
        async def authorize_execute(authorized: str = Form(...), commands: str = Form(...)):
            try:
                is_authorized = authorized.lower() == "true"
                try:
                    cmd_list = json.loads(commands) if commands else []
                except Exception:
                    cmd_list = []
                self._model.submit_web_auth(is_authorized, cmd_list)
                return {"success": True}
            except Exception as e:
                self._model.submit_web_auth(False, [])
                return JSONResponse({"success": False, "error": str(e)}, status_code=500)

        @self._app.post("/api/exec-cmd")
        async def exec_cmd(command: str = Form(...)):
            try:
                result = await self._controller.forwarder.forward(
                    action_to_cmd("exec_cmd"), [command]
                )
                return JSONResponse({"result": result, "error": None})
            except NetworkError as e:
                return JSONResponse({"result": None, "error": str(e)}, status_code=503)

        def _active_or_error():
            agent = self._registry.get_active()
            if agent is None:
                return None, JSONResponse({"error": "No active agent"}, status_code=503)
            return agent, None

        def _is_remote_abs(path: str) -> bool:
            return posixpath.isabs(path) or (
                len(path) >= 3 and path[1] == ":" and path[2] in "/\\"
            )

        async def _ensure_cwd() -> str:
            """Resolve the remote absolute CWD once via get_cwd."""
            if self._cwd is None:
                result = await self._controller.forwarder.forward(action_to_cmd("get_cwd"), [])
                if result.startswith("Error:"):
                    raise NetworkError(result)
                self._cwd = result
            return self._cwd

        async def _join_cwd_async(name: str) -> str:
            cwd = await _ensure_cwd()
            if _is_remote_abs(name) or cwd in (".", ""):
                return name
            return posixpath.join(cwd, name)

        @self._app.get("/api/files/list")
        async def list_files():
            agent, err = _active_or_error()
            if err is not None:
                return err
            try:
                cwd = await _ensure_cwd()
                listing = await self._controller.forwarder.forward(
                    action_to_cmd("list_dir"), [cwd]
                )
                items = _parse_listing(listing)
                return JSONResponse({
                    "current_path": cwd,
                    "items": items,
                    "error": None,
                })
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=503)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self._app.post("/api/files/cwd")
        async def get_cwd():
            try:
                cwd = await _ensure_cwd()
                return {"current_path": cwd}
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=503)

        @self._app.post("/api/files/parent")
        async def parent_dir():
            try:
                base = (await _ensure_cwd()).replace("\\", "/")
                stripped = base.rstrip("/")
                if not stripped:
                    parent = "/" if base.startswith("/") else "."
                elif re.match(r"^[A-Za-z]:$", stripped):
                    parent = stripped + "/"
                else:
                    parent = posixpath.dirname(stripped) or "."
                self._cwd = parent
                return JSONResponse({"current_path": self._cwd, "error": None})
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=503)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self._app.post("/api/files/chdir")
        async def chdir(dirname: str = Form(...)):
            try:
                target = await _join_cwd_async(dirname)
                listing = await self._controller.forwarder.forward(
                    action_to_cmd("list_dir"), [target]
                )
                if listing.startswith("Error:") and "not exist" in listing:
                    return JSONResponse({"error": "Directory not found"}, status_code=404)
                if "is a file" in listing:
                    return JSONResponse({"error": "Not a directory"}, status_code=404)
                self._cwd = target
                return JSONResponse({"current_path": self._cwd, "error": None})
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=503)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self._app.post("/api/files/new")
        async def new_file(filename: str = Form(...)):
            _, err = _active_or_error()
            if err is not None:
                return err
            try:
                file_path = await _join_cwd_async(filename)
                result = await self._controller.forwarder.forward(
                    action_to_cmd("create_file"), [file_path]
                )
                return JSONResponse({"path": file_path, "result": result, "error": None})
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=503)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self._app.post("/api/files/delete")
        async def delete_file(filepath: str = Form(...)):
            _, err = _active_or_error()
            if err is not None:
                return err
            try:
                result = await self._controller.forwarder.forward(
                    action_to_cmd("delete_file"), [await _join_cwd_async(filepath)]
                )
                return JSONResponse({"success": True, "result": result})
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=503)

        @self._app.get("/api/files/download")
        async def download_file(src: str):
            try:
                result = await c2_file_transfer.download(self._registry, await _join_cwd_async(src))
                return JSONResponse({"result": result})
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=503)

        @self._app.post("/api/files/upload")
        async def upload_file(file: UploadFile = File(...), dest: str = Form(...)):
            _, err = _active_or_error()
            if err is not None:
                return err
            try:
                content = await file.read()
                tmp_path = Path(tempfile.gettempdir()) / file.filename
                with open(tmp_path, "wb") as f:
                    f.write(content)
                try:
                    result = await c2_file_transfer.upload(self._registry, str(tmp_path), await _join_cwd_async(dest))
                finally:
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                return JSONResponse({"result": result})
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=503)

        @self._app.post("/api/files/mkdir")
        async def make_dir(dirname: str = Form(...)):
            _, err = _active_or_error()
            if err is not None:
                return err
            try:
                result = await self._controller.forwarder.forward(
                    action_to_cmd("make_dir"), [await _join_cwd_async(dirname)]
                )
                return JSONResponse({"result": result})
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=503)

        @self._app.post("/api/files/copy")
        async def copy_file(src: str = Form(...), dest: str = Form(...)):
            _, err = _active_or_error()
            if err is not None:
                return err
            try:
                result = await self._controller.forwarder.forward(
                    action_to_cmd("copy"), [await _join_cwd_async(src), await _join_cwd_async(dest)]
                )
                return JSONResponse({"result": result})
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=503)

        @self._app.post("/api/files/move")
        async def move_file(src: str = Form(...), dest: str = Form(...)):
            _, err = _active_or_error()
            if err is not None:
                return err
            try:
                result = await self._controller.forwarder.forward(
                    action_to_cmd("move"), [await _join_cwd_async(src), await _join_cwd_async(dest)]
                )
                return JSONResponse({"result": result})
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=503)


def main(config_path: str):
    app = WebApp(config_path)
    print(f"Starting C2 Web Server on http://{app._cfg.listen_host}:{app._cfg.listen_port}")
    print(f"C2 Network Server listening on {app._cfg.c2_host}:{app._cfg.c2_port}")
    uvicorn.run(app._app, host=app._cfg.listen_host, port=app._cfg.listen_port, log_level="info")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Remote Control Tool - C2 Web")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    main(args.config)