"""C2-side ModelModule: orchestrates LLM dialogue and command forwarding.

Per-Agent conversation history is persisted; switching the active
Agent (via /target) switches which history the next chat iteration
uses. Web conversations run as background tasks and are tracked per
Agent: different Agents can hold concurrent sessions that execute
independently (each session serializes its own commands via the
per-Agent instruction lock held by the Forwarder / file transfer).

``chat_async()`` is the CLI entry point. ``begin_chat()`` starts a
background conversation, ``chat_stream()`` is the async generator that
attaches to / follows a running conversation (used by the Web SSE
endpoint) and survives page refresh / client disconnect.
"""

import asyncio
import threading
import time
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


def _new_conversation_state() -> dict:
    return {
        "iteration": 0,
        "phase": "idle",   # idle | llm | exec | auth_wait | done
        "text": "",
        "results": [],
        "pending_command": None,
        "stop": False,
    }


class ModelModule:
    def __init__(self, controller: Controller):
        self._controller = controller
        config = controller.get_config()
        self._llm = LLMClient(config.api_base, config.api_key)
        # CLI stop state (chat_async)
        self._stop_lock = threading.Lock()
        self._stop_requested = False
        # Web: per-Agent conversation state
        self._conv_tasks: Dict[str, asyncio.Task] = {}
        self._conv_states: Dict[str, dict] = {}
        self._streams_lock = threading.Lock()
        self._active_streams: Dict[str, object] = {}
        self._subscribers: set = set()
        self._web_auth_events: Dict[str, threading.Event] = {}
        self._web_auth_results: Dict[str, AuthResult] = {}

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

    async def _llm_stream_async(self, messages: list, agent_id: str):
        """Stream LLM chunks from a worker thread.

        The OpenAI sync client blocks the event loop while its response is
        iterated. Running the producer in a worker thread keeps /api/stop,
        /api/set-auth and the other endpoints responsive during generation.
        The stream is tracked per-Agent so stopping one session never closes
        another session's stream.
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
                self._set_active_stream(agent_id, stream)
                for chunk in stream:
                    if self._stop_requested_for(agent_id):
                        break
                    if chunk.choices and chunk.choices[0].delta.content:
                        loop.call_soon_threadsafe(queue.put_nowait, chunk.choices[0].delta.content)
            except Exception as e:
                if not self._stop_requested_for(agent_id):
                    loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                self._set_active_stream(agent_id, None)
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
            self._close_active_stream(agent_id)
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # ----------------------------------------------------------------
    # CLI chat
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
    # Web: background per-Agent conversation + attach/reconnect
    # ----------------------------------------------------------------
    def conversation_active(self, agent_id: Optional[str] = None) -> bool:
        if agent_id is not None:
            task = self._conv_tasks.get(agent_id)
            return task is not None and not task.done()
        return any(t is not None and not t.done() for t in self._conv_tasks.values())

    def _state(self, agent_id: str) -> dict:
        return self._conv_states.setdefault(agent_id, _new_conversation_state())

    def _stop_requested_for(self, agent_id: str) -> bool:
        st = self._conv_states.get(agent_id)
        return bool(st and st.get("stop"))

    def begin_chat(self, message: str, agent_id: Optional[str] = None) -> Optional[str]:
        """Start a conversation in the background (survives page refresh).

        Returns an error string if the conversation cannot be started,
        otherwise None. The conversation keeps running even if every SSE
        client disconnects; callers re-attach via ``chat_stream()``.
        Different Agents may run conversations concurrently.
        """
        if agent_id is None:
            agent = self._controller.registry.get_active()
            if agent is None:
                return "No active agent. Use /target <id>."
            agent_id = agent.id
        elif self._controller.registry.get(agent_id) is None:
            return f"No such agent: {agent_id}"
        if self.conversation_active(agent_id):
            return f"A conversation with agent '{agent_id}' is already in progress. Please wait for it to finish or press Stop."
        st = self._state(agent_id)
        st.update(_new_conversation_state())
        self._conv_tasks[agent_id] = asyncio.get_running_loop().create_task(
            self._run_conversation(agent_id, message)
        )
        return None

    def _subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def _unsubscribe(self, q) -> None:
        self._subscribers.discard(q)

    def _push_event(self, agent_id: str, ev: dict) -> None:
        ev = dict(ev)
        ev["agent"] = agent_id
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

    async def chat_stream(self, message: Optional[str] = None, agent_id: Optional[str] = None):
        """Async generator feeding the Web SSE stream for one Agent.

        Attaches to the (possibly already running) background conversation of
        ``agent_id`` (defaults to the active Agent): first a snapshot of the
        in-progress state is replayed so a freshly refreshed page can pick up
        where it left off, then live events are forwarded until the
        conversation finishes. ``message`` is accepted for backwards
        compatibility but conversations are started by ``begin_chat()``.
        """
        if agent_id is None:
            agent = self._controller.registry.get_active()
            if agent is None:
                yield {"type": "error", "error": "No active agent. Use /target <id>."}
                return
            agent_id = agent.id
        if message and not self.conversation_active(agent_id):
            err = self.begin_chat(message, agent_id=agent_id)
            if err:
                yield {"type": "error", "error": err}
                return
        if not self.conversation_active(agent_id):
            yield {"type": "done", "iteration": 0}
            return

        q = self._subscribe()
        try:
            st = dict(self._conv_states.get(agent_id) or {})
            if st.get("phase") == "done":
                yield {"type": "done", "iteration": st.get("iteration", 0), "agent": agent_id}
                return
            if st.get("phase") == "llm" and st.get("text"):
                yield {"type": "answering", "iteration": st.get("iteration", 1), "agent": agent_id}
                yield {"type": "chunk", "content": st["text"], "agent": agent_id}
            elif st.get("phase") == "auth_wait" and st.get("pending_command"):
                yield {"type": "auth_required", "commands": [st["pending_command"]], "agent": agent_id}
                yield {"type": "waiting_auth", "iteration": st.get("iteration", 1), "agent": agent_id}
            elif st.get("phase") == "exec" and st.get("results"):
                yield {"type": "executing", "commands": [], "agent": agent_id}
                yield {"type": "execution_done", "results": st["results"], "agent": agent_id}

            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
                    continue
                if ev.get("agent") != agent_id:
                    continue
                yield ev
                if ev.get("type") == "done":
                    return
        finally:
            self._unsubscribe(q)

    async def _run_conversation(self, agent_id: str, message: str) -> None:
        """Background per-Agent conversation loop. Pushes events to
        subscribers instead of yielding, so it survives the SSE client
        disconnecting. Commands are forwarded with ``agent_id`` so the
        conversation keeps targeting its own Agent even after the active
        Agent changes; the per-Agent instruction lock serializes them."""
        st = self._state(agent_id)
        iteration = 0
        stopped = False
        try:
            agent = self._controller.registry.get(agent_id)
            if agent is None:
                self._push_event(agent_id, {"type": "error", "error": f"Agent '{agent_id}' is offline."})
                return
            history = agent.conversation_history
            history.append({"role": "user", "content": message})

            max_iterations = self._controller.get_config().round_limit

            while iteration < max_iterations:
                if self._stop_requested_for(agent_id):
                    stopped = True
                    break
                iteration += 1
                st.update({"iteration": iteration, "phase": "llm", "text": "", "pending_command": None})
                self._push_event(agent_id, {"type": "answering", "iteration": iteration})

                messages = [
                    {"role": "system", "content": self._controller.render_system_prompt_for(agent)}
                ]
                messages.extend(history)

                full_response = ""
                commands: List = []

                try:
                    async for content in self._llm_stream_async(messages, agent_id):
                        full_response += content
                        st["text"] = full_response
                        self._push_event(agent_id, {"type": "chunk", "content": content})

                    history.append({"role": "assistant", "content": full_response})
                    st["text"] = ""

                    if self._stop_requested_for(agent_id):
                        stopped = True
                        break

                    parsed_commands, parse_errors = CommandParser.parse(full_response)
                    if parse_errors:
                        self._push_event(agent_id, {"type": "parse_error", "errors": parse_errors})
                        history.append({
                            "role": "user",
                            "content": f"JSON parse error occurred. Please fix the JSON format and resend:\n{parse_errors}",
                        })
                        continue

                    commands = parsed_commands
                    self._push_event(agent_id, {"type": "response_done", "iteration": iteration, "commands": commands})

                    if not commands:
                        if not parse_errors:
                            break
                        continue

                    all_results = []
                    st.update({"phase": "exec", "results": []})
                    for cmd in commands:
                        if self._stop_requested_for(agent_id):
                            stopped = True
                            break

                        action = cmd.get("action")
                        params_dict = {k: v for k, v in cmd.items() if k != "action"}

                        if action == "exec_cmd" and not check_safety(params_dict.get("command", "")):
                            result_str = "Error: Command blocked due to safety concerns"
                            all_results.append({"action": action, "params": params_dict, "result": result_str})
                            st["results"] = list(all_results)
                            self._push_event(agent_id, {"type": "execution_done", "results": all_results[-1:]})
                            continue

                        if self._controller.get_auth_mode() == 0:
                            st.update({"phase": "auth_wait", "pending_command": cmd})
                            self._push_event(agent_id, {"type": "auth_required", "commands": [cmd]})
                            self._push_event(agent_id, {"type": "waiting_auth", "iteration": iteration})
                            authorized = await self._await_web_auth(agent_id)
                            st["pending_command"] = None
                            st["phase"] = "exec"
                            if self._stop_requested_for(agent_id):
                                stopped = True
                                break
                            if not authorized:
                                self._push_event(agent_id, {"type": "auth_denied", "message": "User denied command execution"})
                                result_str = "Error: User denied command execution"
                                all_results.append({"action": action, "params": params_dict, "result": result_str})
                                st["results"] = list(all_results)
                                self._push_event(agent_id, {"type": "execution_done", "results": all_results[-1:]})
                                continue

                        self._push_event(agent_id, {"type": "executing", "commands": [cmd]})
                        cmd_code = action_to_cmd(action)
                        if cmd_code < 0:
                            result_str = f"Error: Unknown action: {action}"
                        else:
                            params = params_for_action(action, params_dict)
                            try:
                                result_str = await self._controller.forwarder.forward(cmd_code, params, agent_id=agent_id)
                            except NetworkError as e:
                                self._push_event(agent_id, {"type": "error", "error": str(e)})
                                history.append({"role": "user", "content": f"[Network Error] {e}"})
                                return
                        all_results.append({"action": action, "params": params_dict, "result": result_str})
                        st["results"] = list(all_results)
                        self._push_event(agent_id, {"type": "execution_done", "results": all_results[-1:]})

                    if stopped:
                        break

                    result_text = "\n".join(self._format_result(r) for r in all_results)
                    history.append({"role": "user", "content": f"Command execution result:\n{result_text}"})
                except Exception as e:
                    self._push_event(agent_id, {"type": "error", "error": str(e)})
                    break
        except Exception as e:
            try:
                self._push_event(agent_id, {"type": "error", "error": str(e)})
            except Exception:
                pass
        finally:
            st["phase"] = "done"
            if stopped:
                self._push_event(agent_id, {"type": "stopped", "iteration": iteration})
            self._push_event(agent_id, {"type": "done", "iteration": iteration})
            self._conv_tasks.pop(agent_id, None)

    # ----------------------------------------------------------------
    # Web authorization (per Agent)
    # ----------------------------------------------------------------
    async def _await_web_auth(self, agent_id: str) -> bool:
        """Wait for the web client to call /api/authorize-execute for this
        Agent's conversation. Runs the blocking wait in a worker thread so
        the event loop stays free for other endpoints while waiting."""
        self._web_auth_events[agent_id] = threading.Event()
        self._web_auth_results[agent_id] = None
        return await asyncio.to_thread(self._wait_web_auth, agent_id)

    def _wait_web_auth(self, agent_id: str) -> bool:
        ev = self._web_auth_events.get(agent_id)
        if ev is None:
            return False
        deadline = time.time() + 300
        while not ev.is_set():
            if self._stop_requested_for(agent_id):
                return False
            if self._controller.get_auth_mode() == 1:
                return True
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            ev.wait(timeout=min(1.0, remaining))
        if ev.is_set() and self._web_auth_results.get(agent_id) is not None:
            return self._web_auth_results[agent_id].authorized
        return False

    def submit_web_auth(self, authorized: bool, commands: list, agent_id: Optional[str] = None):
        """Called by /api/authorize-execute when the web client decides."""
        if agent_id is None:
            agent_id = self._controller.registry.active_id
        if agent_id is None:
            return
        self._web_auth_results[agent_id] = AuthResult(
            authorized=bool(authorized),
            command=commands[0] if commands else {},
        )
        ev = self._web_auth_events.get(agent_id)
        if ev is not None:
            ev.set()

    # ----------------------------------------------------------------
    # Stop / stream helpers
    # ----------------------------------------------------------------
    def stop(self, agent_id: Optional[str] = None):
        """Request interruption of a conversation (defaults to the active
        Agent's conversation)."""
        if agent_id is None:
            agent_id = self._controller.registry.active_id
        if agent_id is None:
            return
        st = self._conv_states.get(agent_id)
        if st is not None:
            st["stop"] = True
        self._close_active_stream(agent_id)

    def _is_stop_requested(self) -> bool:
        with self._stop_lock:
            return self._stop_requested

    def _set_stop(self, requested: bool):
        with self._stop_lock:
            self._stop_requested = requested

    def _set_active_stream(self, agent_id: str, stream) -> None:
        with self._streams_lock:
            self._active_streams[agent_id] = stream

    def _close_active_stream(self, agent_id: str) -> None:
        with self._streams_lock:
            stream = self._active_streams.get(agent_id)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
                self._active_streams[agent_id] = None

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
        st = self._conv_states.get(agent.id)
        if st is not None:
            st["stop"] = False

    def get_history(self) -> list:
        agent = self._controller.registry.get_active()
        if agent is None:
            return []
        return agent.conversation_history
