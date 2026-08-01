"""C2-side ModelModule: orchestrates LLM dialogue and command forwarding.

Per-Agent conversation history is persisted; switching the active
Agent (via /target) switches which history the next chat iteration
uses. Commands parsed from LLM output are routed to the active Agent
through Controller.forwarder.

``chat()`` is the sync entry point used by the CLI (which schedules it
in a worker thread). Internally it runs ``chat_async()`` via
``asyncio.run`` so the same code path serves both sync and async
callers. ``chat_stream()`` is the async generator used by the Web
SSE endpoint.
"""

import asyncio
from typing import Dict, List, Optional

from src.c2.forwarder import NetworkError
from src.command import action_to_cmd, check_safety, params_for_action
from src.controller import Controller
from src.llm import CommandParser, LLMClient


class ChatResult:
    def __init__(self, response=None, executions=None, error=None):
        self.response = response or ""
        self.executions = executions or []
        self.error = error


class AuthResult:
    def __init__(self, authorized: bool, command: dict = None):
        self.authorized = authorized
        self.command = command or {}


class ModelModule:
    def __init__(self, controller: Controller, mode: str = "cli"):
        self._controller = controller
        self._mode = mode
        config = controller.get_config()
        self._llm = LLMClient(config.api_base, config.api_key)
        self._web_auth_event: Optional[asyncio.Event] = None
        self._web_auth_authorized: bool = False

    def _prompt_auth(self, command: dict) -> AuthResult:
        """Sync CLI prompt; called from a worker thread so input() is safe."""
        action = command.get("action", "")
        params = {k: v for k, v in command.items() if k != "action"}
        print("\n" + "=" * 50)
        print("Command detected:")
        if action == "exec_cmd":
            print(f"  {action}: {params.get('command', '')}")
        elif action == "write_file":
            print(f"  {action}: {params.get('path', '')}")
        else:
            print(f"  {action}: {params}")
        print("-" * 50)
        print("Authorization options: /y /n /y-all /n-all")
        print("-" * 50)
        while True:
            auth = input("\nYour choice: ").strip().lower()
            if auth == "/y":
                return AuthResult(True, command)
            elif auth == "/n":
                return AuthResult(False, command)
            elif auth == "/y-all":
                self._controller.set_auth_mode(1)
                return AuthResult(True, command)
            elif auth == "/n-all":
                self._controller.set_auth_mode(0)
                return AuthResult(False, command)

    def _llm_call_sync(self, messages: list) -> str:
        """Run the (sync, streaming) LLM call and return the full text."""
        stream = self._llm.chat(
            messages=messages,
            model=self._controller.get_config().model,
            stream=True,
        )
        full = ""
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                full += content
                print(content, end="", flush=True)
        print()
        return full

    # ----------------------------------------------------------------
    # Async implementation (shared with chat_stream)
    # ----------------------------------------------------------------
    async def chat_async(self, message: str, stream_to_stdout: bool = False) -> ChatResult:
        agent = self._controller.registry.get_active()
        if agent is None:
            return ChatResult(error="No active agent. Use /agents and /target <id>.")
        history = agent.conversation_history
        history.append({"role": "user", "content": message})

        max_iterations = self._controller.get_config().round_limit
        iteration = 0
        last_response = ""

        while iteration < max_iterations:
            iteration += 1
            messages = [
                {"role": "system", "content": self._controller.render_system_prompt()}
            ]
            messages.extend(history)

            try:
                if stream_to_stdout:
                    full_response = await asyncio.to_thread(self._llm_call_sync, messages)
                else:
                    stream = self._llm.chat(
                        messages=messages,
                        model=self._controller.get_config().model,
                        stream=True,
                    )
                    full_response = ""
                    for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta.content:
                            full_response += chunk.choices[0].delta.content

                if full_response:
                    history.append({"role": "assistant", "content": full_response})
                    last_response = full_response

                parsed_commands, parse_errors = CommandParser.parse(full_response)
                if parse_errors:
                    error_text = "\n".join(parse_errors)
                    if stream_to_stdout:
                        print(f"\n[Parse Error] {error_text}")
                    history.append({
                        "role": "user",
                        "content": f"JSON parse error occurred. Please fix the JSON format and resend:\n{error_text}",
                    })
                    continue

                if not parsed_commands:
                    return ChatResult(response=last_response)

                executions: List = []
                user_denied = False
                for cmd in parsed_commands:
                    action = cmd.get("action")
                    params_dict = {k: v for k, v in cmd.items() if k != "action"}

                    if action == "exec_cmd" and not check_safety(params_dict.get("command", "")):
                        executions.append((f"[{action}]", "Error: Command blocked due to safety concerns"))
                        continue

                    if self._controller.get_auth_mode() == 0:
                        ar = await asyncio.to_thread(self._prompt_auth, cmd)
                        if not ar.authorized:
                            executions.append((f"[{action}]", "Error: User denied command execution"))
                            user_denied = True
                            continue

                    cmd_code = action_to_cmd(action)
                    if cmd_code < 0:
                        executions.append((f"[{action}]", f"Error: Unknown action: {action}"))
                        continue

                    params = params_for_action(action, params_dict)
                    try:
                        result = await self._controller.forwarder.forward(cmd_code, params)
                    except NetworkError as e:
                        history.append({"role": "user", "content": f"[Network Error] {e}. The active Agent is unavailable."})
                        return ChatResult(error=str(e), response=last_response)
                    executions.append((
                        f"[{action}] {params_dict.get('path') or params_dict.get('command') or ''}",
                        result,
                    ))

                if user_denied:
                    history.append({"role": "user", "content": "User denied command execution"})
                    return ChatResult(response=last_response, executions=executions)

                result_text = "\n".join(f"{a}\n{r}" for a, r in executions)
                history.append({
                    "role": "user",
                    "content": f"Command execution result:\n{result_text}",
                })
            except Exception as e:
                return ChatResult(error=str(e), response=last_response)

        return ChatResult(response=last_response)

    # ----------------------------------------------------------------
    # Web SSE streaming (yields events instead of returning ChatResult)
    # ----------------------------------------------------------------
    async def chat_stream(self, message: str):
        agent = self._controller.registry.get_active()
        if agent is None:
            yield {"type": "error", "error": "No active agent. Use /target <id>."}
            return
        history = agent.conversation_history
        history.append({"role": "user", "content": message})

        max_iterations = self._controller.get_config().round_limit
        iteration = 0
        last_response = ""

        while iteration < max_iterations:
            iteration += 1
            yield {"type": "answering", "iteration": iteration}

            messages = [
                {"role": "system", "content": self._controller.render_system_prompt()}
            ]
            messages.extend(history)

            full_response = ""
            commands: List = []

            try:
                stream = self._llm.chat(
                    messages=messages,
                    model=self._controller.get_config().model,
                    stream=True,
                )
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        full_response += content
                        yield {"type": "chunk", "content": content}

                history.append({"role": "assistant", "content": full_response})
                last_response = full_response

                parsed_commands, parse_errors = CommandParser.parse(full_response)
                if parse_errors:
                    yield {"type": "parse_error", "errors": parse_errors}
                    history.append({
                        "role": "user",
                        "content": f"JSON parse error occurred. Please fix the JSON format and resend:\n{parse_errors}",
                    })
                    continue

                commands = parsed_commands
                yield {"type": "response_done", "iteration": iteration, "commands": commands}

                if not commands:
                    if not parse_errors:
                        break
                    continue

                all_results = []
                user_denied = False
                for cmd in commands:
                    action = cmd.get("action")
                    params_dict = {k: v for k, v in cmd.items() if k != "action"}

                    if action == "exec_cmd" and not check_safety(params_dict.get("command", "")):
                        result_str = "Error: Command blocked due to safety concerns"
                        all_results.append({"action": action, "params": params_dict, "result": result_str})
                        yield {"type": "execution_done", "results": all_results[-1:]}
                        continue

                    if self._controller.get_auth_mode() == 0:
                        yield {"type": "auth_required", "commands": [cmd]}
                        yield {"type": "waiting_auth", "iteration": iteration}
                        authorized = await self._await_web_auth()
                        if not authorized:
                            yield {"type": "auth_denied", "message": "User denied command execution"}
                            user_denied = True
                            break

                    cmd_code = action_to_cmd(action)
                    if cmd_code < 0:
                        result_str = f"Error: Unknown action: {action}"
                    else:
                        params = params_for_action(action, params_dict)
                        try:
                            result_str = await self._controller.forwarder.forward(cmd_code, params)
                        except NetworkError as e:
                            yield {"type": "error", "error": str(e)}
                            history.append({"role": "user", "content": f"[Network Error] {e}"})
                            return
                    all_results.append({"action": action, "params": params_dict, "result": result_str})
                    yield {"type": "execution_done", "results": all_results[-1:]}

                if user_denied:
                    history.append({"role": "user", "content": "User denied command execution"})
                    break

                result_text = "\n".join(self._format_result(r) for r in all_results)
                history.append({"role": "user", "content": f"Command execution result:\n{result_text}"})
            except Exception as e:
                yield {"type": "error", "error": str(e)}
                break

        yield {"type": "done", "iteration": iteration}

    async def _await_web_auth(self) -> bool:
        """Wait for the web client to call /api/authorize-execute."""
        self._web_auth_event = asyncio.Event()
        self._web_auth_authorized = False
        try:
            await asyncio.wait_for(self._web_auth_event.wait(), timeout=300)
            return self._web_auth_authorized
        except asyncio.TimeoutError:
            return False
        finally:
            self._web_auth_event = None

    def submit_web_auth(self, authorized: bool, commands: list):
        """Called by /api/authorize-execute when the web client decides."""
        self._web_auth_authorized = bool(authorized)
        if self._web_auth_event is not None:
            self._web_auth_event.set()

    @staticmethod
    def _format_result(ex: dict) -> str:
        params = ex.get("params", {})
        action = ex.get("action", "")
        detail = params.get("command", "") if action == "exec_cmd" else params.get("path", "")
        return f"[{action}] [{detail}]\n{ex.get('result', '')}"

    def reset_conversation(self):
        agent = self._controller.registry.get_active()
        if agent is None:
            return
        agent.conversation_history = []
        self._controller.reset_auth()

    def get_history(self) -> list:
        agent = self._controller.registry.get_active()
        if agent is None:
            return []
        return agent.conversation_history

    def get_all_histories(self) -> Dict[str, list]:
        return {info.id: list(info.conversation_history)
                for info in self._controller.registry.list_all()}