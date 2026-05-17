"""Generic installer：不动文件，只打印 shell 接入指令。

适用于：任何遵守 ``ANTHROPIC_BASE_URL`` 环境变量的 Anthropic-SDK 客户端
（``anthropic`` Python 包、``@anthropic-ai/sdk`` Node 包、Hermes / Openclaw
发行版等），且我们暂时没有专门的 installer。
"""

from __future__ import annotations

from telos.init.base import AgentInstaller, InstallResult


class GenericInstaller(AgentInstaller):
    name = "generic"

    def install(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="install")
        r.notes.append(
            "在启动 agent 之前 export 以下环境变量（写进 shell rc 文件以持久化）：\n"
            f"    export ANTHROPIC_BASE_URL={self.proxy_url}\n"
            "如客户端用的是 OpenAI shape（如 telos / mini_swe_runner），"
            "把 base_url 显式传给 OpenAI client，或设 OPENAI_BASE_URL。"
        )
        return r

    def uninstall(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="uninstall")
        r.notes.append(
            "从你的 shell rc 中删除 ANTHROPIC_BASE_URL 的 export（如有），"
            "或在当前 shell 执行：unset ANTHROPIC_BASE_URL"
        )
        return r

    def status(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="status")
        r.notes.append(
            "generic 模式不持有状态；以当前 shell 的 $ANTHROPIC_BASE_URL 为准。"
        )
        return r
