"""C2-side controller.

Owns the Config, AgentRegistry, and Forwarder. Provides placeholders
for the system_prompt that get filled with the active Agent's OS on
each chat invocation.
"""

import threading

from src.c2.agent_registry import AgentRegistry
from src.c2.forwarder import Forwarder
from src.command import is_low_risk, requires_auth
from src.config import Config

VALID_AUTH_MODES = (0, 1, 2)


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
        return self.render_system_prompt_for(self._registry.get_active())

    def render_system_prompt_for(self, agent) -> str:
        """Return system_prompt with {system_name} replaced by the given Agent's OS."""
        os_name = agent.os if agent is not None else "Unknown"
        return self._config.system_prompt.replace("{system_name}", os_name)

    def get_auth_mode(self) -> int:
        with self._lock:
            return self._auth_mode

    def set_auth_mode(self, mode: int):
        if mode not in VALID_AUTH_MODES:
            return
        with self._lock:
            self._auth_mode = mode

    def requires_auth(self, action: str) -> bool:
        """Whether ``action`` needs explicit authorization under the current mode."""
        return requires_auth(self.get_auth_mode(), action)

    def is_low_risk(self, action: str) -> bool:
        return is_low_risk(action)

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