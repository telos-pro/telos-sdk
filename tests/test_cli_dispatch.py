"""``telos.cli`` dispatch tests: subcommand routing / error codes / alias persistence."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile

from telos import cli


def _iso_home() -> None:
    os.environ["TELOS_HOME"] = tempfile.mkdtemp(prefix="telos-cli-")


def _run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def test_help_omits_proxy() -> None:
    rc, out = _run(["--help"])
    assert rc == 0
    assert "telos gateway" in out
    # proxy is a hidden alias and does not appear in the subcommands list.
    assert "  proxy " not in out
    print("✓ test_help_omits_proxy")


def test_unknown_subcommand() -> None:
    rc, out = _run(["bogus"])
    assert rc == 2
    assert "unknown subcommand" in out
    print("✓ test_unknown_subcommand")


def test_alias_round_trip() -> None:
    _iso_home()
    rc, _ = _run(["alias", "codex"])
    assert rc == 0
    rc, out = _run(["alias"])
    assert rc == 0
    assert "codex" in out
    print("✓ test_alias_round_trip")


def test_alias_rejects_unknown() -> None:
    _iso_home()
    rc, out = _run(["alias", "nope"])
    assert rc == 2
    assert "unknown harness" in out
    print("✓ test_alias_rejects_unknown")


def test_mode_persists_without_gateway() -> None:
    _iso_home()
    rc, out = _run(["mode", "both"])
    assert rc == 0
    assert "both" in out
    import telos.config as cfgmod
    assert cfgmod.load_config().mode == "both"
    print("✓ test_mode_persists_without_gateway")


def test_mode_rejects_bad_label() -> None:
    _iso_home()
    rc, out = _run(["mode", "turbo"])
    assert rc == 2
    assert "unknown mode" in out
    print("✓ test_mode_rejects_bad_label")


def main() -> None:
    test_help_omits_proxy()
    test_unknown_subcommand()
    test_alias_round_trip()
    test_alias_rejects_unknown()
    test_mode_persists_without_gateway()
    test_mode_rejects_bad_label()
    print("\nall cli dispatch tests passed.")


if __name__ == "__main__":
    main()
