"""Installer 基类与统一返回结构。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InstallResult:
    """Installer 操作结果，给 CLI / 测试统一格式。"""

    agent: str
    action: str                              # "install" / "uninstall"
    changed_files: list[Path] = field(default_factory=list)
    backups: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    already_installed: bool = False


class AgentInstaller(ABC):
    """每个 agent 一个 installer。

    Installer 只对配置文件做幂等 patch：``install`` 多次 = 一次；
    ``uninstall`` 严格还原到 install 之前的状态（用 ``.bak`` 备份）。
    """

    name: str = "base"  # 子类覆盖

    def __init__(self, *, proxy_url: str = "http://127.0.0.1:7171") -> None:
        self.proxy_url = proxy_url

    @abstractmethod
    def install(self) -> InstallResult: ...

    @abstractmethod
    def uninstall(self) -> InstallResult: ...

    @abstractmethod
    def status(self) -> InstallResult: ...
