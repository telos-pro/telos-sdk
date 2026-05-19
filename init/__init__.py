"""TELOS agent connectors —— ``python -m telos.init``.

One installer per harness: makes the harness point ``ANTHROPIC_BASE_URL`` /
``OPENAI_BASE_URL`` at the local gateway when it starts.

Supported harnesses:

- ``claude-code``: patches the ``env`` field of ``~/.claude/settings.json``
- ``codex``:       environment variable injection (``OPENAI_BASE_URL``)
- ``openclaw``:    environment variable injection (``ANTHROPIC_BASE_URL``)
- ``hermes``:      environment variable injection (``ANTHROPIC_BASE_URL``)
- ``generic``:     prints a set of shell export commands for the user to add to
  their rc file
"""

from __future__ import annotations

from typing import Callable

from telos.init.anthropic_env import make_env_installer
from telos.init.base import AgentInstaller, InstallResult
from telos.init.claude_code import ClaudeCodeInstaller
from telos.init.generic import GenericInstaller

# name → factory; the factory signature is uniformly ``(*, proxy_url=...) -> AgentInstaller``.
InstallerFactory = Callable[..., AgentInstaller]

INSTALLERS: dict[str, InstallerFactory] = {
    "claude-code": ClaudeCodeInstaller,
    "codex": make_env_installer("codex", "OPENAI_BASE_URL"),
    "openclaw": make_env_installer("openclaw", "ANTHROPIC_BASE_URL"),
    "hermes": make_env_installer("hermes", "ANTHROPIC_BASE_URL"),
    "generic": GenericInstaller,
}

__all__ = ["AgentInstaller", "InstallResult", "INSTALLERS", "InstallerFactory"]
