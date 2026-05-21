"""Launcher-based installer for harnesses that do not have a stable config-file
hook for ``ANTHROPIC_BASE_URL`` / ``OPENAI_BASE_URL`` (codex / openclaw / hermes).

Design choice: these harnesses keep their routing inside their own structured
config (e.g. ``~/.openclaw/openclaw.json``'s ``models.providers.*``, or
``~/.hermes/config.yaml``'s ``providers.*.model.base_url``), and ignore a plain
``ANTHROPIC_BASE_URL`` environment variable. Rather than fragilely patching
those files, telos routes them through the gateway by **only** the launcher
path —— ``telos <harness>`` injects the env into the subprocess at startup
(see ``telos/cli.py::_cmd_launch_harness``). Users who invoke the harness CLI
directly are expected to configure the gateway address themselves through the
harness's own config UI (e.g. ``openclaw config set models.providers.…`` or
``hermes config set model.base_url …``).

Therefore this installer changes no files. ``install()`` / ``uninstall()`` are
informational; ``status()`` checks the real preconditions of the launcher
path —— executable on ``PATH``, gateway running, and the live process env ——
so the user can verify "would `telos {harness}` actually work right now?".
"""

from __future__ import annotations

import os

from telos.init.base import AgentInstaller, InstallResult


class EnvInstaller(AgentInstaller):
    """Parameterized environment variable installer, instantiated by ``name`` + ``env_var``.

    Persists nothing. The harness is wired to the gateway only when launched
    via ``telos <name>``; ``status()`` verifies that path's preconditions
    without claiming false connection state.
    """

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

    # ------------------------------------------------------------------
    # install / uninstall (no file changes)
    # ------------------------------------------------------------------

    def install(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="install")
        r.notes.append(
            f"`telos {self.name}` injects {self.env_var}={self.proxy_url} into "
            f"the subprocess at launch — this is the supported path."
        )
        r.notes.append(
            f"if you invoke `{self.name}` directly, configure the gateway in "
            f"its own config (telos does NOT patch ~/.{self.name}/* — that file "
            f"is yours to own)."
        )
        return r

    def uninstall(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="uninstall")
        r.notes.append(
            f"telos holds no persistent state for {self.name}; nothing to undo."
        )
        return r

    # ------------------------------------------------------------------
    # status (checks the real preconditions of the launcher path)
    # ------------------------------------------------------------------

    def status(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="status")

        exe_path = self._which_executable()
        if exe_path is None:
            r.notes.append(
                f"executable: NOT found on PATH — `telos {self.name}` "
                f"cannot launch it"
            )
        else:
            r.notes.append(f"executable: {exe_path}")

        gateway_url = self._gateway_url()
        if gateway_url is None:
            r.notes.append(
                "gateway: not running — start it with `telos gateway start` "
                f"(or `telos {self.name}` will auto-start one)"
            )
        else:
            r.notes.append(f"gateway: running → {gateway_url}")

        live = os.environ.get(self.env_var)
        target = gateway_url or self.proxy_url
        if live is None:
            r.notes.append(
                f"current shell: {self.env_var} is not set (this is normal — "
                f"the launcher injects it per-subprocess)"
            )
        elif live == target:
            r.notes.append(
                f"current shell: {self.env_var}={live} (points at telos)"
            )
        else:
            r.notes.append(
                f"current shell: {self.env_var}={live} (NOT the telos gateway "
                f"{target})"
            )

        # "already_installed" reflects the launcher path being usable —— the
        # only thing telos itself is on the hook for.
        r.already_installed = exe_path is not None
        return r

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _which_executable(self) -> str | None:
        """Locate the harness executable using telos's own override-aware lookup."""
        from telos.config import load_config
        from telos.harnesses import HARNESS_SPECS, executable_path

        spec = HARNESS_SPECS.get(self.name)
        if spec is None:
            # codex/openclaw/hermes are all in HARNESS_SPECS — but be defensive.
            import shutil
            return shutil.which(self.name)
        cfg = load_config()
        return executable_path(spec, cfg.harness_executables)

    def _gateway_url(self) -> str | None:
        """Return the live gateway base URL if a daemon is running, else ``None``."""
        try:
            from telos.gateway import daemon
        except ImportError:
            return None
        state = daemon.read_state()
        return state.base_url() if state is not None else None


def make_env_installer(name: str, env_var: str):
    """Return an installer factory bound to ``name`` / ``env_var`` (same signature as other installers)."""
    def factory(*, proxy_url: str = "http://127.0.0.1:7171") -> EnvInstaller:
        return EnvInstaller(name=name, env_var=env_var, proxy_url=proxy_url)
    return factory
