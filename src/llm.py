"""LLM client and JSON command parser.

Originally from sample Localaw; retained verbatim because the C2/Agent
split does not change how the LLM is invoked or how JSON code blocks
are parsed.
"""

import json
import re
from typing import Any, Dict, List, Tuple

from openai import OpenAI


class CommandParser:
    PATTERN = re.compile(r"```json\s*\n?(.*?)\n?```", re.DOTALL)

    _VALID_ESCAPES = set('"\\/bfnrtu')

    @staticmethod
    def _repair_json(json_str: str) -> str:
        result = []
        i = 0
        n = len(json_str)
        in_string = False

        while i < n:
            ch = json_str[i]

            if not in_string:
                result.append(ch)
                if ch == '"':
                    in_string = True
                i += 1
                continue

            if ch == '\\':
                if i + 1 < n and json_str[i + 1] in CommandParser._VALID_ESCAPES:
                    result.append(ch)
                    result.append(json_str[i + 1])
                    i += 2
                else:
                    result.append('\\\\')
                    i += 1
                continue

            if ch == '"':
                j = i + 1
                while j < n and json_str[j] in ' \t\n\r':
                    j += 1
                is_closing = False
                if j >= n:
                    is_closing = True
                elif json_str[j] in '}]':
                    is_closing = True
                elif json_str[j] == ':':
                    is_closing = True
                elif json_str[j] == ',':
                    k = j + 1
                    while k < n and json_str[k] in ' \t\n\r':
                        k += 1
                    if k < n and json_str[k] == '"':
                        is_closing = True
                if is_closing:
                    result.append(ch)
                    in_string = False
                else:
                    result.append('\\"')
                i += 1
                continue

            result.append(ch)
            i += 1

        return ''.join(result)

    @staticmethod
    def parse(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
        matches = CommandParser.PATTERN.findall(text)
        commands = []
        errors = []

        for match in matches:
            raw = match.strip()

            if not raw or raw[0] not in '{[':
                continue

            cmd = None

            try:
                cmd = json.loads(raw)
            except json.JSONDecodeError:
                try:
                    repaired = CommandParser._repair_json(raw)
                    cmd = json.loads(repaired)
                except json.JSONDecodeError as e2:
                    snippet = raw[:200] + "..." if len(raw) > 200 else raw
                    errors.append(
                        f"JSON parse failed: {e2}. Snippet: {snippet}"
                    )
                    continue

            if cmd is not None:
                if isinstance(cmd, dict) and "action" in cmd:
                    commands.append(cmd)
                elif isinstance(cmd, list):
                    for c in cmd:
                        if isinstance(c, dict) and "action" in c:
                            commands.append(c)

        return commands, errors


class LLMClient:
    def __init__(self, api_base: str, api_key: str):
        self.client = OpenAI(base_url=api_base, api_key=api_key)

    def chat(
        self, messages: list, model: str, temperature: float = 0.7, stream: bool = False
    ):
        return self.client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, stream=stream
        )