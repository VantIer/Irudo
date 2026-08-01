"""C2-side controller.

Owns the Config, AgentRegistry, and Forwarder. Provides placeholders
for the system_prompt that get filled with the active Agent's OS on
each chat invocation.
"""

import threading

from src.c2.agent_registry import AgentRegistry
from src.c2.forwarder import Forwarder
from src.config import Config


class Controller:
    def __init__(self, config: Config, registry: AgentRegistry):
        self._config = config
        self._registry = registry
        self._auth_mode = config.auth_mode
        self._auth_mode_initial = self._auth_mode
        self._forwarder: Forwarder = Forwarder(registry, cmd_timeout=config.cmd_timeout)
        self._lock = threading.Lock()

    @property
    def system_name(self) -> str:
        agent = self._registry.get_active()
        if agent is not None:
            return agent.os
        return "Unknown"

    def render_system_prompt(self) -> str:
        """Return system_prompt with {system_name} replaced by the active Agent's OS."""
        agent = self._registry.get_active()
        os_name = agent.os if agent is not None else "Unknown"
        return self._config.system_prompt.replace("{system_name}", os_name)

    def get_auth_mode(self) -> int:
        with self._lock:
            return self._auth_mode

    def set_auth_mode(self, mode: int):
        with self._lock:
            self._auth_mode = mode

    def get_config(self) -> Config:
        return self._config

    @property
    def forwarder(self) -> Forwarder:
        return self._forwarder

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    def reset_auth(self):
        with self._lock:
            self._auth_mode = self._auth_mode_initial