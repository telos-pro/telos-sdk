"""``telos.init`` env-based installers tests (codex / openclaw / hermes)."""

from __future__ import annotations

from telos.init import INSTALLERS
from telos.init.anthropic_env import EnvInstaller


def test_env_installers_registered() -> None:
    for name in ("codex", "openclaw", "hermes"):
        assert name in INSTALLERS
    print("✓ test_env_installers_registered")


def test_install_notes_mention_env_var() -> None:
    inst = EnvInstaller(name="codex", env_var="OPENAI_BASE_URL",
                        proxy_url="http://127.0.0.1:7171")
    r = inst.install()
    assert r.agent == "codex"
    assert any("OPENAI_BASE_URL" in n for n in r.notes)
    assert not r.changed_files  # env installer does not modify files
    print("✓ test_install_notes_mention_env_var")


def test_factory_from_registry() -> None:
    inst = INSTALLERS["openclaw"](proxy_url="http://h:1")
    r = inst.install()
    assert r.agent == "openclaw"
    assert any("ANTHROPIC_BASE_URL" in n for n in r.notes)
    print("✓ test_factory_from_registry")


def main() -> None:
    test_env_installers_registered()
    test_install_notes_mention_env_var()
    test_factory_from_registry()
    print("\nall env installer tests passed.")


if __name__ == "__main__":
    main()
