"""STELA agent 接入器 —— ``python -m stela.init --agent <name>``。

每个 agent 一个 installer 模块，参照 RTK 的 ``src/hooks/init.rs`` 的
``run_*_mode`` 模式。Installer 只做一件事：让 agent 在启动时把
``ANTHROPIC_BASE_URL`` 指向本地代理。

支持的 agent：

- ``claude-code``: patch ``~/.claude/settings.json`` 的 ``env`` 字段
- ``generic``:    打印一份 shell export 指令，由用户自行加入 rc 文件
"""

from stela.init.base import AgentInstaller, InstallResult
from stela.init.claude_code import ClaudeCodeInstaller
from stela.init.generic import GenericInstaller

INSTALLERS: dict[str, type[AgentInstaller]] = {
    "claude-code": ClaudeCodeInstaller,
    "generic": GenericInstaller,
}

__all__ = ["AgentInstaller", "InstallResult", "INSTALLERS"]
