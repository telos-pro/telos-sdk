"""Generic installer: touches no files, only prints shell connection commands.

Applicable to: any Anthropic-SDK client that respects the ``ANTHROPIC_BASE_URL``
environment variable (the ``anthropic`` Python package, the ``@anthropic-ai/sdk``
Node package, the Hermes / Openclaw distributions, etc.) for which we do not yet
have a dedicated installer.
"""

from __future__ import annotations

from telos.init.base import AgentInstaller, InstallResult


class GenericInstaller(AgentInstaller):
    name = "generic"

    def install(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="install")
        r.notes.append(
            "Export the following environment variables before starting the agent "
            "(write them into your shell rc file to persist):\n"
            f"    export ANTHROPIC_BASE_URL={self.proxy_url}\n"
            "If the client uses the OpenAI shape (e.g. telos / mini_swe_runner), "
            "pass base_url explicitly to the OpenAI client, or set OPENAI_BASE_URL."
        )
        return r

    def uninstall(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="uninstall")
        r.notes.append(
            "Remove the ANTHROPIC_BASE_URL export from your shell rc (if any), "
            "or run in the current shell: unset ANTHROPIC_BASE_URL"
        )
        return r

    def status(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="status")
        r.notes.append(
            "Generic mode holds no state; the current shell's $ANTHROPIC_BASE_URL is authoritative."
        )
        return r
