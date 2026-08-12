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
import threading
import time
from typing import List, Optional

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
    def __init__(self, controller: Controller):
        self._controller = controller
        config = controller.get_config()
        self._llm = LLMClient(config.api_base, config.api_key)
        self._web_auth_event: Optional[threading.Event] = None
        self._web_auth_result: Optional[AuthResult] = None
        self._stop_lock = threading.Lock()
        self._stop_requested = False
        self._active_stream_lock = threading.Lock()
        self._active_stream = None
        # Background conversation task, decoupled from the SSE client so a
        # page refresh / client disconnect does not abort the conversation.
        self._conv_task: Optional[asyncio.Task] = None
        self._conv_state: dict = {
            "iteration": 0,
            "phase": "idle",   # idle | llm | exec | auth_wait | done
            "text": "",
            "results": [],
            "pending_command": None,
        }
        self._subscribers: set = set()

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

    def _llm_call_sync(self, messages: list, stream_to_stdout: bool = True) -> str:
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
                if stream_to_stdout:
                    print(content, end="", flush=True)
        if stream_to_stdout:
            print()
        return full

    async def _llm_stream_async(self, messages: list):
        """Stream LLM chunks from a worker thread.

        The OpenAI sync client blocks the event loop while its response is
        iterated. Running the producer in a worker thread keeps /api/stop,
        /api/set-auth and the other endpoints responsive during generation.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _producer():
            stream = None
            try:
                stream = self._llm.chat(
                    messages=messages,
                    model=self._controller.get_config().model,
                    stream=True,
                )
                self._set_active_stream(stream)
                for chunk in stream:
                    if self._is_stop_requested():
                        break
                    if chunk.choices and chunk.choices[0].delta.content:
                        loop.call_soon_threadsafe(queue.put_nowait, chunk.choices[0].delta.content)
            except Exception as e:
                if not self._is_stop_requested():
                    loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                self._set_active_stream(None)
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                loop.call_soon_threadsafe(queue.put_nowait, None)

        task = asyncio.create_task(asyncio.to_thread(_producer))
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self._close_active_stream()
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # ----------------------------------------------------------------
    # Async implementation (shared with chat_stream)
    # ----------------------------------------------------------------
    async def chat_async(self, message: str, stream_to_stdout: bool = False) -> ChatResult:
        self._set_stop(False)
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
                full_response = await asyncio.to_thread(self._llm_call_sync, messages, stream_to_stdout)

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
    # Web SSE: background conversation + attach/reconnect
    # ----------------------------------------------------------------
    def conversation_active(self) -> bool:
        task = self._conv_task
        return task is not None and not task.done()

    def begin_chat(self, message: str) -> Optional[str]:
        """Start a conversation in the background (survives page refresh).

        Returns an error string if the conversation cannot be started,
        otherwise None. The conversation keeps running even if every SSE
        client disconnects; callers re-attach via ``chat_stream()``.
        """
        agent = self._controller.registry.get_active()
        if agent is None:
            return "No active agent. Use /target <id>."
        if self.conversation_active():
            return "A conversation is already in progress. Please wait for it to finish or press Stop."
        self._set_stop(False)
        self._conv_state = {
            "iteration": 0,
            "phase": "idle",
            "text": "",
            "results": [],
            "pending_command": None,
        }
        self._conv_task = asyncio.get_running_loop().create_task(
            self._run_conversation(message)
        )
        return None

    def _subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def _unsubscribe(self, q) -> None:
        self._subscribers.discard(q)

    def _push_event(self, ev: dict) -> None:
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    async def chat_stream(self, message: Optional[str] = None):
        """Async generator feeding the Web SSE stream.

        Attaches to the (possibly already running) background conversation:
        first a snapshot of the in-progress state is replayed so a freshly
        refreshed page can pick up where it left off, then live events are
        forwarded until the conversation finishes. ``message`` is accepted
        for backwards compatibility but conversations are started by
        ``begin_chat()``.
        """
        if message and not self.conversation_active():
            err = self.begin_chat(message)
            if err:
                yield {"type": "error", "error": err}
                return
        if not self.conversation_active():
            yield {"type": "done", "iteration": 0}
            return

        q = self._subscribe()
        try:
            st = dict(self._conv_state)
            if st.get("phase") == "done":
                yield {"type": "done", "iteration": st.get("iteration", 0)}
                return
            if st.get("phase") == "llm" and st.get("text"):
                yield {"type": "answering", "iteration": st.get("iteration", 1)}
                yield {"type": "chunk", "content": st["text"]}
            elif st.get("phase") == "auth_wait" and st.get("pending_command"):
                yield {"type": "auth_required", "commands": [st["pending_command"]]}
                yield {"type": "waiting_auth", "iteration": st.get("iteration", 1)}
            elif st.get("phase") == "exec" and st.get("results"):
                yield {"type": "executing", "commands": []}
                yield {"type": "execution_done", "results": st["results"]}

            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
                    continue
                yield ev
                if ev.get("type") == "done":
                    return
        finally:
            self._unsubscribe(q)

    async def _run_conversation(self, message: str) -> None:
        """Background conversation loop. Pushes events to subscribers instead
        of yielding, so it survives the SSE client disconnecting."""
        self._set_stop(False)
        iteration = 0
        stopped = False
        try:
            agent = self._controller.registry.get_active()
            if agent is None:
                self._push_event({"type": "error", "error": "No active agent. Use /target <id>."})
                return
            history = agent.conversation_history
            history.append({"role": "user", "content": message})

            max_iterations = self._controller.get_config().round_limit

            while iteration < max_iterations:
                if self._is_stop_requested():
                    stopped = True
                    break
                iteration += 1
                self._conv_state.update({
                    "iteration": iteration,
                    "phase": "llm",
                    "text": "",
                    "pending_command": None,
                })
                self._push_event({"type": "answering", "iteration": iteration})

                messages = [
                    {"role": "system", "content": self._controller.render_system_prompt()}
                ]
                messages.extend(history)

                full_response = ""
                commands: List = []

                try:
                    async for content in self._llm_stream_async(messages):
                        full_response += content
                        self._conv_state["text"] = full_response
                        self._push_event({"type": "chunk", "content": content})

                    history.append({"role": "assistant", "content": full_response})
                    self._conv_state["text"] = ""

                    if self._is_stop_requested():
                        stopped = True
                        break

                    parsed_commands, parse_errors = CommandParser.parse(full_response)
                    if parse_errors:
                        self._push_event({"type": "parse_error", "errors": parse_errors})
                        history.append({
                            "role": "user",
                            "content": f"JSON parse error occurred. Please fix the JSON format and resend:\n{parse_errors}",
                        })
                        continue

                    commands = parsed_commands
                    self._push_event({"type": "response_done", "iteration": iteration, "commands": commands})

                    if not commands:
                        if not parse_errors:
                            break
                        continue

                    all_results = []
                    self._conv_state.update({"phase": "exec", "results": []})
                    for cmd in commands:
                        if self._is_stop_requested():
                            stopped = True
                            break

                        action = cmd.get("action")
                        params_dict = {k: v for k, v in cmd.items() if k != "action"}

                        if action == "exec_cmd" and not check_safety(params_dict.get("command", "")):
                            result_str = "Error: Command blocked due to safety concerns"
                            all_results.append({"action": action, "params": params_dict, "result": result_str})
                            self._conv_state["results"] = list(all_results)
                            self._push_event({"type": "execution_done", "results": all_results[-1:]})
                            continue

                        if self._controller.get_auth_mode() == 0:
                            self._conv_state.update({"phase": "auth_wait", "pending_command": cmd})
                            self._push_event({"type": "auth_required", "commands": [cmd]})
                            self._push_event({"type": "waiting_auth", "iteration": iteration})
                            authorized = await self._await_web_auth()
                            self._conv_state["pending_command"] = None
                            self._conv_state["phase"] = "exec"
                            if self._is_stop_requested():
                                stopped = True
                                break
                            if not authorized:
                                self._push_event({"type": "auth_denied", "message": "User denied command execution"})
                                result_str = "Error: User denied command execution"
                                all_results.append({"action": action, "params": params_dict, "result": result_str})
                                self._conv_state["results"] = list(all_results)
                                self._push_event({"type": "execution_done", "results": all_results[-1:]})
                                continue

                        self._push_event({"type": "executing", "commands": [cmd]})
                        cmd_code = action_to_cmd(action)
                        if cmd_code < 0:
                            result_str = f"Error: Unknown action: {action}"
                        else:
                            params = params_for_action(action, params_dict)
                            try:
                                result_str = await self._controller.forwarder.forward(cmd_code, params)
                            except NetworkError as e:
                                self._push_event({"type": "error", "error": str(e)})
                                history.append({"role": "user", "content": f"[Network Error] {e}"})
                                return
                        all_results.append({"action": action, "params": params_dict, "result": result_str})
                        self._conv_state["results"] = list(all_results)
                        self._push_event({"type": "execution_done", "results": all_results[-1:]})

                    if stopped:
                        break

                    result_text = "\n".join(self._format_result(r) for r in all_results)
                    history.append({"role": "user", "content": f"Command execution result:\n{result_text}"})
                except Exception as e:
                    self._push_event({"type": "error", "error": str(e)})
                    break
        except Exception as e:
            try:
                self._push_event({"type": "error", "error": str(e)})
            except Exception:
                pass
        finally:
            self._conv_state["phase"] = "done"
            if stopped:
                self._push_event({"type": "stopped", "iteration": iteration})
            self._push_event({"type": "done", "iteration": iteration})
            self._conv_task = None

    async def _await_web_auth(self) -> bool:
        """Wait for the web client to call /api/authorize-execute.

        Runs the blocking wait in a worker thread so the event loop stays
        free for other endpoints (set-auth / stop / file ops) while a
        command is awaiting authorization.
        """
        self._web_auth_event = threading.Event()
        self._web_auth_result = None
        return await asyncio.to_thread(self._wait_web_auth)

    def _wait_web_auth(self) -> bool:
        if self._web_auth_event is None:
            return False
        deadline = time.time() + 300
        while not self._web_auth_event.is_set():
            if self._is_stop_requested():
                return False
            if self._controller.get_auth_mode() == 1:
                return True
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self._web_auth_event.wait(timeout=min(1.0, remaining))
        if self._web_auth_event.is_set() and self._web_auth_result is not None:
            return self._web_auth_result.authorized
        return False

    def submit_web_auth(self, authorized: bool, commands: list):
        """Called by /api/authorize-execute when the web client decides."""
        self._web_auth_result = AuthResult(
            authorized=bool(authorized),
            command=commands[0] if commands else {},
        )
        if self._web_auth_event is not None:
            self._web_auth_event.set()

    def stop(self):
        """Request interruption of the current conversation loop."""
        self._set_stop(True)
        self._close_active_stream()

    def _is_stop_requested(self) -> bool:
        with self._stop_lock:
            return self._stop_requested

    def _set_stop(self, requested: bool):
        with self._stop_lock:
            self._stop_requested = requested

    def _set_active_stream(self, stream) -> None:
        with self._active_stream_lock:
            self._active_stream = stream

    def _get_active_stream(self):
        with self._active_stream_lock:
            return self._active_stream

    def _close_active_stream(self) -> None:
        stream = self._get_active_stream()
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

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
        self._set_stop(False)

    def get_history(self) -> list:
        agent = self._controller.registry.get_active()
        if agent is None:
            return []
        return agent.conversation_history