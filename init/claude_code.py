"""Claude Code (npm 全局发行版) installer。

接入方式：往 ``~/.claude/settings.json`` 的 ``env`` 字段写
``ANTHROPIC_BASE_URL=<proxy_url>``，Claude Code 启动时会自动把该字段注入
进程环境。等价于用户每次启动前手动 ``export ANTHROPIC_BASE_URL=...``，
但不污染 shell rc，也不依赖 PATH wrapper。

不接触 npm 包本体；升级 Claude Code 也不会丢配置。

幂等性：
- ``install`` 多次 = 一次。
- 已存在用户自定义 ``ANTHROPIC_BASE_URL`` 时，挪到 ``__stela_previous_base_url``，
  install 期间用我们的值覆盖，uninstall 时还原。
- 完整原始 settings.json 还会备份到 ``settings.json.stela.bak``（首次 install
  时一次性写入；后续重复 install 不动）。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stela.init.base import AgentInstaller, InstallResult


_STELA_MARK_KEY = "__stela_installed"
_PREVIOUS_KEY = "__stela_previous_base_url"
_BASE_URL_KEY = "ANTHROPIC_BASE_URL"


def _default_settings_path() -> Path:
    # 与 Claude Code 文档一致：~/.claude/settings.json
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"


@dataclass
class _LoadedSettings:
    path: Path
    data: dict[str, Any]
    existed: bool


def _load(path: Path) -> _LoadedSettings:
    if not path.exists():
        return _LoadedSettings(path=path, data={}, existed=False)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"{path} 不是合法 JSON（{e}）。请先备份并手动修复后再运行 stela init。"
        ) from e
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} 顶层必须是 JSON object，实际是 {type(data).__name__}")
    return _LoadedSettings(path=path, data=data, existed=True)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class ClaudeCodeInstaller(AgentInstaller):
    name = "claude-code"

    def __init__(
        self,
        *,
        proxy_url: str = "http://127.0.0.1:7171",
        settings_path: Path | None = None,
    ) -> None:
        super().__init__(proxy_url=proxy_url)
        self.settings_path = settings_path or _default_settings_path()

    # ------------------------------------------------------------------
    # install
    # ------------------------------------------------------------------

    def install(self) -> InstallResult:
        loaded = _load(self.settings_path)
        env = loaded.data.setdefault("env", {})
        if not isinstance(env, dict):
            raise RuntimeError(
                f"{self.settings_path} 的 env 字段必须是 object，实际是 {type(env).__name__}"
            )

        result = InstallResult(agent=self.name, action="install")

        current = env.get(_BASE_URL_KEY)
        already_ours = env.get(_STELA_MARK_KEY) is True and current == self.proxy_url
        if already_ours:
            result.already_installed = True
            result.notes.append(f"已接入 STELA 代理 ({current})；无操作")
            return result

        # 首次 patch：备份原文件
        if loaded.existed:
            backup = self.settings_path.with_suffix(self.settings_path.suffix + ".stela.bak")
            if not backup.exists():
                shutil.copy2(self.settings_path, backup)
                result.backups.append(backup)

        # 保留用户原有 ANTHROPIC_BASE_URL（如有）
        if current is not None and current != self.proxy_url:
            env[_PREVIOUS_KEY] = current
            result.notes.append(f"保留原 ANTHROPIC_BASE_URL ({current}) 到 {_PREVIOUS_KEY}")

        env[_BASE_URL_KEY] = self.proxy_url
        env[_STELA_MARK_KEY] = True

        _atomic_write(self.settings_path, loaded.data)
        result.changed_files.append(self.settings_path)
        result.notes.append(
            f"已写入 {self.settings_path}：env.ANTHROPIC_BASE_URL = {self.proxy_url}"
        )
        return result

    # ------------------------------------------------------------------
    # uninstall
    # ------------------------------------------------------------------

    def uninstall(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="uninstall")
        if not self.settings_path.exists():
            result.notes.append(f"{self.settings_path} 不存在；无操作")
            return result

        loaded = _load(self.settings_path)
        env = loaded.data.get("env")
        if not isinstance(env, dict) or env.get(_STELA_MARK_KEY) is not True:
            result.notes.append("settings.json 没有 STELA 标记；无操作")
            return result

        env.pop(_STELA_MARK_KEY, None)
        previous = env.pop(_PREVIOUS_KEY, None)
        if previous is not None:
            env[_BASE_URL_KEY] = previous
            result.notes.append(f"还原 ANTHROPIC_BASE_URL = {previous}")
        else:
            env.pop(_BASE_URL_KEY, None)
            result.notes.append("移除 ANTHROPIC_BASE_URL")

        if not env:
            loaded.data.pop("env", None)

        _atomic_write(self.settings_path, loaded.data)
        result.changed_files.append(self.settings_path)
        return result

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> InstallResult:
        result = InstallResult(agent=self.name, action="status")
        if not self.settings_path.exists():
            result.notes.append(f"{self.settings_path} 不存在")
            return result
        loaded = _load(self.settings_path)
        env = loaded.data.get("env") or {}
        if env.get(_STELA_MARK_KEY) is True:
            result.already_installed = True
            result.notes.append(
                f"已接入 STELA 代理：{env.get(_BASE_URL_KEY)}"
            )
            if _PREVIOUS_KEY in env:
                result.notes.append(f"原值会在 uninstall 时还原：{env[_PREVIOUS_KEY]}")
        elif _BASE_URL_KEY in env:
            result.notes.append(
                f"settings.json 已设 ANTHROPIC_BASE_URL = {env[_BASE_URL_KEY]}（非 STELA 注入）"
            )
        else:
            result.notes.append("settings.json 未注入 ANTHROPIC_BASE_URL")
        return result
