"""Installer base class and unified return structure."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InstallResult:
    """Installer operation result, providing a unified format for the CLI / tests."""

    agent: str
    action: str                              # "install" / "uninstall"
    changed_files: list[Path] = field(default_factory=list)
    backups: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    already_installed: bool = False


class AgentInstaller(ABC):
    """One installer per agent.

    Installers only apply idempotent patches to config files: running ``install``
    multiple times is equivalent to running it once; ``uninstall`` strictly restores
    the state from before ``install`` (using ``.bak`` backups).
    """

    name: str = "base"  # overridden by subclasses

    def __init__(self, *, proxy_url: str = "http://127.0.0.1:7171") -> None:
        self.proxy_url = proxy_url

    @abstractmethod
    def install(self) -> InstallResult: ...

    @abstractmethod
    def uninstall(self) -> InstallResult: ...

    @abstractmethod
    def status(self) -> InstallResult: ...
