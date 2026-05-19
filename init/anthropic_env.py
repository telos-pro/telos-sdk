"""Generic installer based on environment variable injection.

Applicable to codex / openclaw / hermes —— these harnesses do not have a stable,
patchable config file like claude-code does; they are connected by giving the
process environment a ``ANTHROPIC_BASE_URL`` / ``OPENAI_BASE_URL`` pointing at the
gateway.

Two ways to take effect:

1. Injected directly by the telos launcher when ``telos <harness>`` starts the
   subprocess (recommended, zero config).
2. The user writes the ``export`` into their shell rc themselves —— ``install()``
   prints this command.

Therefore this installer changes no files; it only returns a descriptive
``InstallResult``.
"""

from __future__ import annotations

from telos.init.base import AgentInstaller, InstallResult


class EnvInstaller(AgentInstaller):
    """Parameterized environment variable installer, instantiated by ``name`` + ``env_var``."""

    def __init__(
        self,
        *,
        name: str,
        env_var: str = "ANTHROPIC_BASE_URL",
        proxy_url: str = "http://127.0.0.1:7171",
    ) -> None:
        super().__init__(proxy_url=proxy_url)
        self.name = name
        self.env_var = env_var

    def install(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="install")
        r.notes.append(
            f"`telos {self.name}` automatically injects {self.env_var}={self.proxy_url} on startup."
        )
        r.notes.append(
            f"To also route through the gateway outside the telos launcher, add this line to your shell rc:\n"
            f"    export {self.env_var}={self.proxy_url}"
        )
        return r

    def uninstall(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="uninstall")
        r.notes.append(
            f"telos holds no state for {self.name}; if you exported {self.env_var} manually, "
            f"just remove it from your shell rc."
        )
        return r

    def status(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="status")
        r.notes.append(
            f"{self.name} uses environment variable injection ({self.env_var}); it is set by "
            f"the telos launcher at launch time, with no persistent state."
        )
        return r


def make_env_installer(name: str, env_var: str):
    """Return an installer factory bound to ``name`` / ``env_var`` (same signature as other installers)."""
    def factory(*, proxy_url: str = "http://127.0.0.1:7171") -> EnvInstaller:
        return EnvInstaller(name=name, env_var=env_var, proxy_url=proxy_url)
    return factory
