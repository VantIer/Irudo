"""Configuration loader.

A single Config class reads all known fields from JSON; C2-specific and
remote-specific fields are simply ignored if not present (or vice versa).
"""

import json
import os
import sys
import tempfile
from pathlib import Path


class Config:
    def __init__(self, config_path: str = "config.json"):
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            base_dir = Path(sys._MEIPASS)
        else:
            base_dir = Path(__file__).parent.parent

        self.config_path = base_dir / config_path

        self.api_base: str = "http://localhost:11434/v1"
        self.api_key: str = "ollama"
        self.model: str = "llama3.2"
        self.round_limit: int = 20
        self.cmd_timeout: int = 60
        self.auth_mode: int = 0
        self.system_prompt: str = "You are a remote AI assistant with the ability to execute commands on the user's remote system.\n\nCURRENT OPERATING SYSTEM: {system_name}\n\nYou have access to the following commands:\n\n1. get_cwd: Get the absolute current working directory of the remote system\n   - params: (none)\n\n2. list_dir: List directory contents\n   - params: path (optional, default: current directory)\n\n3. read_file: Read file contents\n   - params: path (required), start_line (optional, from 1, 0 or empty means whole file), end_line (optional)\n   - example: {\"action\": \"read_file\", \"path\": \"file.txt\", \"start_line\": 1, \"end_line\": 10}\n\n4. write_file: Write content to a file\n   - params: path (required), content (required)\n\n5. delete_file: Delete files or directories\n   - params: path (required)\n\n6. create_file: Create an empty file\n   - params: path (required)\n\n7. make_dir: Create a directory\n   - params: path (required, including path)\n   - example: {\"action\": \"make_dir\", \"path\": \"dir/new_folder\"}\n\n8. delete_dir: Delete a directory\n   - params: path (required, including path)\n\n9. rename_dir: Rename a directory\n   - params: path (required, including path), new_name (required, directory name only)\n   - example: {\"action\": \"rename_dir\", \"path\": \"dir/old_name\", \"new_name\": \"new_name\"}\n\n10. rename_file: Rename a file\n    - params: path (required, including path), new_name (required, file name only)\n    - example: {\"action\": \"rename_file\", \"path\": \"dir/old.txt\", \"new_name\": \"new.txt\"}\n\n11. edit_file: Edit file content (add, delete, or modify lines)\n    - params: path (required), operation (required: add/del/modify), start_line (required), end_line (required for del/modify), content (required for add/modify)\n    - operation \"add\": Insert content at start_line position\n    - operation \"del\": Delete lines from start_line to end_line (inclusive)\n    - operation \"modify\": Delete lines from start_line to end_line (inclusive), then insert content at start_line position\n    - example: {\"action\": \"edit_file\", \"path\": \"file.txt\", \"operation\": \"add\", \"start_line\": 5, \"end_line\": 0, \"content\": \"new line\"}\n    - example: {\"action\": \"edit_file\", \"path\": \"file.txt\", \"operation\": \"del\", \"start_line\": 3, \"end_line\": 5}\n    - example: {\"action\": \"edit_file\", \"path\": \"file.txt\", \"operation\": \"modify\", \"start_line\": 1, \"end_line\": 3, \"content\": \"replacement\"}\n\n12. copy: Copy a file or directory\n    - params: src (required), dest (required)\n    - example: {\"action\": \"copy\", \"src\": \"dir/source.txt\", \"dest\": \"dir/target.txt\"}\n\n13. move: Move a file or directory\n    - params: src (required), dest (required)\n    - example: {\"action\": \"move\", \"src\": \"dir/source.txt\", \"dest\": \"dir/target.txt\"}\n\n14. exec_cmd: Execute shell commands\n    - params: command (required)\n\nSTRICT EXECUTION RULES - YOUR BEHAVIOR DEPENDS ON FOLLOWING THESE:\n\n1. ONE COMMAND AT A TIME - ALWAYS\n   - You MUST only produce ONE JSON command block in your response\n   - NEVER send multiple commands in a single response\n   - NEVER include more than one ```json code block per response\n\n2. WAIT FOR RESULT BEFORE NEXT\n   - After sending ONE command, you MUST wait for the execution result\n   - Only after receiving the result, you may send the next command\n   - Do not speculate about command results\n\n3. COMMAND RESULT HANDLING\n   - When you receive a command result, acknowledge it briefly\n   - If more work is needed, send the next single command\n   - If the task is complete, respond normally in plain text\n\n4. USER DENIAL\n   - If the user denies a command, you will receive: \"User denied command execution\"\n   - When you receive this message, acknowledge the denial and respond in plain text\n   - Do NOT retry the denied command\n   - Ask if the user wants to do something else instead\n\nCONTENT RULES:\n- NEVER respond with JSON code blocks unless you are submitting a command for execution\n- For normal conversation, questions, or displaying information, ALWAYS use plain text only\n- Do NOT use JSON blocks for examples, demonstrations, explanations, or any other purpose\n\nCOMMAND SUBMISSION:\nWhen you need to execute a command:\n```json\n{\"action\": \"list_dir\", \"path\": \".\"}\n```\n\nOr:\n```json\n{\"action\": \"read_file\", \"path\": \"filename.txt\", \"start_line\": 1, \"end_line\": 20}\n```\n\nOr:\n```json\n{\"action\": \"exec_cmd\", \"command\": \"dir\"}\n```\n\nAlways respond in the same language as the user's query."

        self.listen_host: str = "127.0.0.1"
        self.listen_port: int = 8880

        self.c2_host: str = "0.0.0.0"
        self.c2_port: int = 8881
        self.c2_auth_tokens: str = ""
        self.heartbeat_timeout_sec: int = 60

        self.heartbeat_interval_sec: int = 30
        self.reconnect_initial_sec: float = 1.0
        self.reconnect_max_sec: float = 60.0

        # Temp dirs for web file transfer. Empty means:
        #   dl_temp_dir -> <C2 working dir>/downloads (auto-created)
        #   ul_temp_dir -> the environment default temp directory
        self.dl_temp_dir: str = ""
        self.ul_temp_dir: str = ""

        self.load()

    def load(self):
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.api_base = data.get("api_base", self.api_base)
            self.api_key = data.get("api_key", self.api_key)
            self.model = data.get("model", self.model)
            self.round_limit = data.get("round_limit", self.round_limit)
            self.cmd_timeout = data.get("cmd_timeout", self.cmd_timeout)
            self.auth_mode = data.get("auth_mode", self.auth_mode)
            self.system_prompt = data.get("system_prompt", self.system_prompt)
            self.listen_host = data.get("listen_host", self.listen_host)
            self.listen_port = data.get("listen_port", self.listen_port)
            self.c2_host = data.get("c2_host", self.c2_host)
            self.c2_port = data.get("c2_port", self.c2_port)
            token_val = data.get("c2_auth_tokens", self.c2_auth_tokens)
            if isinstance(token_val, (list, tuple)):
                if len(token_val) > 1:
                    raise ValueError(
                        "config error: 'c2_auth_tokens' must be a single value "
                        f"(found a list of {len(token_val)}). "
                        "Configure exactly one shared token."
                    )
                token_val = token_val[0] if token_val else ""
            self.c2_auth_tokens = str(token_val or "")
            self.heartbeat_timeout_sec = data.get("heartbeat_timeout_sec", self.heartbeat_timeout_sec)
            self.heartbeat_interval_sec = data.get("heartbeat_interval_sec", self.heartbeat_interval_sec)
            self.reconnect_initial_sec = data.get("reconnect_initial_sec", self.reconnect_initial_sec)
            self.reconnect_max_sec = data.get("reconnect_max_sec", self.reconnect_max_sec)
            self.dl_temp_dir = str(data.get("dl_temp_dir", self.dl_temp_dir) or "")
            self.ul_temp_dir = str(data.get("ul_temp_dir", self.ul_temp_dir) or "")

    def get_dl_dir(self) -> Path:
        """Download staging directory (files land here after a download)."""
        if self.dl_temp_dir.strip():
            p = Path(self.dl_temp_dir).expanduser()
        else:
            p = Path(os.getcwd()) / "downloads"
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return p

    def get_ul_dir(self) -> Path:
        """Upload staging directory (web-uploaded bytes are buffered here)."""
        if self.ul_temp_dir.strip():
            p = Path(self.ul_temp_dir).expanduser()
        else:
            p = Path(tempfile.gettempdir())
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return p