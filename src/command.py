"""C2-side command router.

Converts an LLM-emitted JSON command dict into a numeric cmd code (per
protocol.ACTION_CMDS) and a list of positional parameters. Performs a
safety check on shell commands.

Local execution is no longer done here - all action commands are
forwarded to the active Agent via Forwarder.
"""

from typing import List

from common.protocol import ACTION_CMDS, ACTION_NAME_TO_CMD


FORBIDDEN_PATTERNS = ["rm -rf /"]


def check_safety(cmd: str) -> bool:
    cmd_lower = cmd.lower()
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.lower() in cmd_lower:
            return False
    return True


def action_to_cmd(action: str) -> int:
    return ACTION_NAME_TO_CMD.get(action, -1)


def params_for_action(action: str, cmd_dict: dict) -> List[str]:
    """Extract ordered param list per ACTION_CMDS schema."""
    if action not in ACTION_NAME_TO_CMD:
        return []
    cmd_code = ACTION_NAME_TO_CMD[action]
    _, names = ACTION_CMDS[cmd_code]
    out = []
    for name in names:
        v = cmd_dict.get(name, "")
        if v is None:
            v = ""
        out.append(str(v))
    return out