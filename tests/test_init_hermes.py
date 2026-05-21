"""HermesInstaller tests.

The installer patches ``~/.hermes/config.yaml``'s top-level
``model.base_url`` to route through telos and mirrors the original URL into
``~/.telos/config.json`` upstreams. Uses a temp config + ``TELOS_HOME``
override to avoid touching real user files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from telos.init.hermes import HermesInstaller


def _sample_hermes_config() -> dict[str, Any]:
    return {
        "model": {
            "default": "deepseek/deepseek-chat",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        },
        "providers": {
            "aiclaudexyz": {
                "model": {
                    "default": "anthropic/claude-opus-4.6",
                    "base_url": "https://api.aiclaude.xyz/",
                    "api_key": "sk-test",
                    "api_mode": "chat_completions",
                },
            },
        },
        "agent": {"max_turns": 90},
    }


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _make_inst(tmp_path: Path) -> tuple[HermesInstaller, Path, Path]:
    config_path = tmp_path / "hermes" / "config.yaml"
    _write_yaml(config_path, _sample_hermes_config())
    state_path = tmp_path / "telos-state" / "hermes.json"
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    inst = HermesInstaller(
        proxy_url="http://127.0.0.1:7171",
        config_path=config_path,
        state_path=state_path,
    )
    return inst, config_path, state_path


def _restore_env() -> None:
    os.environ.pop("TELOS_HOME", None)


def test_install_patches_model_base_url(tmp_path: Path) -> None:
    inst, config_path, state_path = _make_inst(tmp_path)
    try:
        r = inst.install()
        assert config_path in r.changed_files
        data = yaml.safe_load(config_path.read_text())
        assert (data["model"]["base_url"]
                == "http://127.0.0.1:7171/upstreams/openrouter")
        # Untouched fields stay put.
        assert data["model"]["api_mode"] == "chat_completions"
        assert data["model"]["provider"] == "openrouter"
        assert data["providers"]["aiclaudexyz"]["model"]["api_key"] == "sk-test"
        state = json.loads(state_path.read_text())
        assert state["version"] == 2
        recs = state["patched"]
        primary_rec = next(r for r in recs if r["key"] == "__primary__")
        assert primary_rec["previous_base_url"] == "https://openrouter.ai/api/v1"
        assert primary_rec["provider_id"] == "openrouter"
    finally:
        _restore_env()


def test_install_mirrors_url_into_telos_upstreams(tmp_path: Path) -> None:
    inst, _config_path, _state = _make_inst(tmp_path)
    try:
        inst.install()
        telos_cfg_path = Path(os.environ["TELOS_HOME"]) / "config.json"
        assert telos_cfg_path.exists()
        telos_data = json.loads(telos_cfg_path.read_text())
        upstream = telos_data["upstreams"]["openrouter"]
        assert upstream["url"] == "https://openrouter.ai/api/v1"
        assert upstream["protocol"] == "openai-chat"
        assert upstream["engine"] == "deepseek"
    finally:
        _restore_env()


def test_install_is_idempotent(tmp_path: Path) -> None:
    inst, _config_path, _state = _make_inst(tmp_path)
    try:
        inst.install()
        r2 = inst.install()
        assert r2.already_installed is True
        assert not r2.changed_files
    finally:
        _restore_env()


def test_install_creates_backup_on_first_run(tmp_path: Path) -> None:
    inst, config_path, _state = _make_inst(tmp_path)
    try:
        r = inst.install()
        backup = config_path.with_suffix(config_path.suffix + ".telos.bak")
        assert backup.exists()
        assert backup in r.backups
        backed_up = yaml.safe_load(backup.read_text())
        assert (backed_up["model"]["base_url"]
                == "https://openrouter.ai/api/v1")
    finally:
        _restore_env()


def test_uninstall_restores_original(tmp_path: Path) -> None:
    inst, config_path, state_path = _make_inst(tmp_path)
    try:
        inst.install()
        r = inst.uninstall()
        assert config_path in r.changed_files
        data = yaml.safe_load(config_path.read_text())
        assert data["model"]["base_url"] == "https://openrouter.ai/api/v1"
        assert not state_path.exists()
    finally:
        _restore_env()


def test_uninstall_skips_when_user_manually_changed(tmp_path: Path) -> None:
    inst, config_path, state_path = _make_inst(tmp_path)
    try:
        inst.install()
        # User manually changes base_url after install.
        data = yaml.safe_load(config_path.read_text())
        data["model"]["base_url"] = "https://something-else"
        config_path.write_text(yaml.safe_dump(data, sort_keys=False))

        r = inst.uninstall()
        assert not r.changed_files
        data2 = yaml.safe_load(config_path.read_text())
        assert data2["model"]["base_url"] == "https://something-else"
        assert state_path.exists()
    finally:
        _restore_env()


def test_status_reports_connected(tmp_path: Path) -> None:
    inst, _config_path, _state = _make_inst(tmp_path)
    try:
        inst.install()
        r = inst.status()
        assert r.already_installed is True
        assert any("upstreams/openrouter" in n for n in r.notes)
    finally:
        _restore_env()


def test_status_reports_not_installed(tmp_path: Path) -> None:
    inst, _config_path, _state = _make_inst(tmp_path)
    try:
        r = inst.status()
        assert r.already_installed is False
        assert any("direct →" in n for n in r.notes)
    finally:
        _restore_env()


def test_install_no_config_file(tmp_path: Path) -> None:
    inst = HermesInstaller(
        config_path=tmp_path / "nonexistent" / "config.yaml",
        state_path=tmp_path / "state.json",
    )
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        r = inst.install()
        assert not r.changed_files
        assert any("does not exist" in n for n in r.notes)
    finally:
        _restore_env()


def test_install_anthropic_api_mode_uses_anthropic_upstream(tmp_path: Path) -> None:
    """When api_mode=anthropic_messages, the registered upstream uses the
    anthropic protocol + engine."""
    cfg = _sample_hermes_config()
    cfg["model"]["api_mode"] = "anthropic_messages"
    cfg["model"]["provider"] = "claude-anthropic"
    cfg["model"]["base_url"] = "https://api.anthropic.com"
    config_path = tmp_path / "config.yaml"
    _write_yaml(config_path, cfg)
    os.environ["TELOS_HOME"] = str(tmp_path / "telos-home")
    try:
        inst = HermesInstaller(
            config_path=config_path,
            state_path=tmp_path / "state.json",
        )
        inst.install()
        telos_data = json.loads(
            (Path(os.environ["TELOS_HOME"]) / "config.json").read_text()
        )
        upstream = telos_data["upstreams"]["claude-anthropic"]
        assert upstream["protocol"] == "anthropic-messages"
        assert upstream["engine"] == "anthropic"
    finally:
        _restore_env()
