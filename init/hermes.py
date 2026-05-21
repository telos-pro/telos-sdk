"""Hermes installer.

Routes the **top-level active model** through the local telos gateway by
patching ``model.base_url`` in ``~/.hermes/config.yaml``. Alternative
provider entries under ``providers.*.model.*`` are left alone — telos lists
them in the install output so the user can opt in by re-running with
``providers_to_patch=[<key>...]`` (using the synthetic key ``__primary__``
for the top-level block, or the provider id for nested entries).

Re-install behavior on daemon URL drift mirrors the openclaw installer:
if the current ``base_url`` is a stale telos route, the original URL is
recovered from the state file rather than overwritten.

Caveat: PyYAML round-trip does NOT preserve comments; the original is
backed up to ``config.yaml.telos.bak`` on first install.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml

from telos.config import (
    TelosConfig,
    UpstreamConfig,
    load_config,
    save_config,
    telos_home,
)
from telos.init.base import AgentInstaller, InstallResult


_DEFAULT_GATEWAY_URL = "http://127.0.0.1:7171"
_STATE_VERSION = 2
_PRIMARY_KEY = "__primary__"  # synthetic id for the top-level model block

_TELOS_ROUTE_RE = re.compile(r"^https?://[^/]+/upstreams/[A-Za-z0-9_\-.]+/?$")


def _looks_like_telos_route(url: str) -> bool:
    return bool(_TELOS_ROUTE_RE.match(url))


def _default_config_path() -> Path:
    home = os.environ.get("HERMES_HOME")
    return (Path(home) if home else Path.home() / ".hermes") / "config.yaml"


def _default_state_path() -> Path:
    return telos_home() / "installer-state" / "hermes.json"


def _atomic_write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                       default_flow_style=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _classify_api_mode(api_mode: str) -> tuple[str, str]:
    if api_mode == "anthropic_messages":
        return "anthropic-messages", "anthropic"
    return "openai-chat", "deepseek"


@dataclass
class _Target:
    key: str           # ``__primary__`` for the top-level model block
    provider_id: str   # slug used for the telos upstream
    base_url: str
    api_mode: str


def _all_target_keys(data: dict[str, Any]) -> list[str]:
    """All discoverable patchable locations (top-level + per-provider)."""
    out: list[str] = []
    model = data.get("model")
    if isinstance(model, dict) and isinstance(model.get("base_url"), str):
        out.append(_PRIMARY_KEY)
    providers = data.get("providers")
    if isinstance(providers, dict):
        for pid, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            sub = entry.get("model")
            if not isinstance(sub, dict):
                continue
            if isinstance(sub.get("base_url"), str) and sub["base_url"]:
                out.append(str(pid))
    return out


def _target_info(data: dict[str, Any], key: str) -> _Target | None:
    if key == _PRIMARY_KEY:
        model = data.get("model")
        if not isinstance(model, dict):
            return None
        provider = model.get("provider")
        base_url = model.get("base_url")
        api_mode = model.get("api_mode") or "chat_completions"
        if (not isinstance(provider, str) or not provider
                or not isinstance(base_url, str) or not base_url):
            return None
        return _Target(key=_PRIMARY_KEY, provider_id=str(provider),
                        base_url=str(base_url), api_mode=str(api_mode))

    providers = data.get("providers")
    if not isinstance(providers, dict):
        return None
    entry = providers.get(key)
    if not isinstance(entry, dict):
        return None
    sub = entry.get("model")
    if not isinstance(sub, dict):
        return None
    base_url = sub.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        return None
    api_mode = sub.get("api_mode") or "chat_completions"
    return _Target(key=key, provider_id=key, base_url=base_url,
                    api_mode=str(api_mode))


def _apply_patch(data: dict[str, Any], key: str, new_url: str) -> None:
    if key == _PRIMARY_KEY:
        data["model"]["base_url"] = new_url
    else:
        data["providers"][key]["model"]["base_url"] = new_url


def _read_state_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(raw, dict) and isinstance(raw.get("patched"), list):
        return [r for r in raw["patched"] if isinstance(r, dict)]
    if isinstance(raw, dict) and "provider_id" in raw:
        return [{**raw, "key": _PRIMARY_KEY}]
    return []


def _write_state_file(path: Path, patched: Sequence[dict[str, Any]]) -> None:
    _atomic_write_json(path, {"version": _STATE_VERSION,
                              "patched": list(patched)})


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------

class HermesInstaller(AgentInstaller):
    name = "hermes"

    def __init__(
        self,
        *,
        proxy_url: str = _DEFAULT_GATEWAY_URL,
        config_path: Path | None = None,
        state_path: Path | None = None,
        providers_to_patch: Sequence[str] | None = None,
    ) -> None:
        super().__init__(proxy_url=proxy_url)
        self._config_path = config_path or _default_config_path()
        self._state_path = state_path or _default_state_path()
        self._providers_to_patch = (
            list(providers_to_patch) if providers_to_patch is not None else None
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _read_config(self) -> dict[str, Any] | None:
        if not self._config_path.exists():
            return None
        loaded = yaml.safe_load(self._config_path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None

    def _telos_route_url(self, slug: str) -> str:
        return f"{self.proxy_url.rstrip('/')}/upstreams/{slug}"

    def _ensure_upstream_slug(
        self, telos_cfg: TelosConfig, slug: str, url: str, api_mode: str,
    ) -> bool:
        protocol, engine = _classify_api_mode(api_mode)
        existing = telos_cfg.upstreams.get(slug)
        # Strip any /vN version suffix: the gateway constructs {url}/{tail} where
        # tail already includes the version (e.g. "v1/chat/completions") because the
        # OpenAI SDK adds it when the patched base_url has no /vN suffix.
        normalized_url = re.sub(r"/v\d+$", "", url.rstrip("/"))
        desired = UpstreamConfig(url=normalized_url, engine=engine,
                                  protocol=protocol, via=self.name)
        if existing == desired:
            return False
        telos_cfg.upstreams[slug] = desired
        return True

    # ------------------------------------------------------------------
    # install
    # ------------------------------------------------------------------

    def install(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="install")

        data = self._read_config()
        if data is None:
            r.notes.append(
                f"{self._config_path} does not exist (or is not a YAML "
                f"mapping). Run hermes onboarding first (`hermes model`)."
            )
            return r

        all_keys = _all_target_keys(data)
        if not all_keys:
            r.notes.append("no model.base_url found in config.yaml")
            return r

        if self._providers_to_patch is not None:
            target_keys = list(self._providers_to_patch)
        elif _PRIMARY_KEY in all_keys:
            target_keys = [_PRIMARY_KEY]
        else:
            r.notes.append(
                "top-level model.base_url not set; configure a primary model "
                "in hermes (`hermes model`) first."
            )
            return r

        targets: list[_Target] = []
        for k in target_keys:
            info = _target_info(data, k)
            if info is None:
                r.notes.append(
                    f"key {k!r} not found or missing base_url; skipping"
                )
                continue
            targets.append(info)

        if not targets:
            r.notes.append("nothing to patch.")
            return r

        existing_state = _read_state_file(self._state_path)
        state_by_key = {s["key"]: s for s in existing_state if "key" in s}

        # Resolve each target: figure out (info, original_url, needs_patch).
        # The two are independent — telos config might need a refresh (e.g.
        # to add the new ``via`` field on upgrade) even when config.yaml is
        # already routed correctly.
        resolved: list[tuple[_Target, str, bool]] = []
        skipped_stale_no_state: list[str] = []

        for t in targets:
            route_url = self._telos_route_url(t.provider_id)
            existing_rec = state_by_key.get(t.key)

            if t.base_url == route_url:
                if existing_rec is None:
                    continue
                resolved.append((t, existing_rec["previous_base_url"], False))
            elif _looks_like_telos_route(t.base_url):
                if existing_rec is None:
                    skipped_stale_no_state.append(t.key)
                    continue
                resolved.append((t, existing_rec["previous_base_url"], True))
            else:
                resolved.append((t, t.base_url, True))

        for k in skipped_stale_no_state:
            loc = "model.base_url" if k == _PRIMARY_KEY else f"providers.{k}.model.base_url"
            r.notes.append(
                f"{loc} is a stale telos route but state file has no record "
                f"of the original. Edit it back to the real upstream URL and "
                f"re-run install."
            )

        if not resolved:
            r.notes.append("nothing to patch.")
            return r

        # Always refresh telos upstream slugs so via stays current on upgrade.
        telos_cfg = load_config()
        telos_changed = False
        for info, original_url, _ in resolved:
            if self._ensure_upstream_slug(telos_cfg, info.provider_id,
                                           original_url, info.api_mode):
                telos_changed = True
                r.notes.append(
                    f"telos upstream `{info.provider_id}` → {original_url}"
                )
        if telos_changed:
            telos_path = save_config(telos_cfg)
            r.changed_files.append(telos_path)

        to_patch = [(info, orig) for info, orig, needs in resolved if needs]
        if not to_patch:
            for info, _, _ in resolved:
                loc = ("model.base_url" if info.key == _PRIMARY_KEY
                       else f"providers.{info.key}.model.base_url")
                r.notes.append(f"{loc} already routed; no change.")
            r.already_installed = not telos_changed
            return r

        backup = self._config_path.with_suffix(
            self._config_path.suffix + ".telos.bak"
        )
        if not backup.exists():
            shutil.copy2(self._config_path, backup)
            r.backups.append(backup)

        for info, original_url in to_patch:
            route_url = self._telos_route_url(info.provider_id)
            _apply_patch(data, info.key, route_url)
            state_by_key[info.key] = {
                "key": info.key,
                "provider_id": info.provider_id,
                "previous_base_url": original_url,
                "gateway_route_url": route_url,
            }
            loc = ("model.base_url" if info.key == _PRIMARY_KEY
                   else f"providers.{info.key}.model.base_url")
            r.notes.append(f"patched {loc} → {route_url}")

        _atomic_write_yaml(self._config_path, data)
        r.changed_files.append(self._config_path)
        _write_state_file(self._state_path, list(state_by_key.values()))

        patched_keys = {info.key for info, _ in to_patch}
        patched_keys.update(info.key for info, _, needs in resolved if not needs)
        other_keys = [k for k in all_keys if k not in patched_keys]
        if other_keys:
            labels = ["model" if k == _PRIMARY_KEY else f"providers.{k}.model"
                       for k in other_keys]
            r.notes.append(
                f"other routes detected ({', '.join(labels)}); not patched."
            )
        r.notes.append(
            f"note: PyYAML round-trip does not preserve comments; the "
            f"original is at {backup}."
        )
        return r

    # ------------------------------------------------------------------
    # uninstall
    # ------------------------------------------------------------------

    def uninstall(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="uninstall")

        records = _read_state_file(self._state_path)
        if not records:
            r.notes.append(
                f"no telos installer state for hermes "
                f"({self._state_path}); nothing to undo."
            )
            return r

        data = self._read_config()
        if data is None:
            r.notes.append(
                f"{self._config_path} does not exist; deleting stale state."
            )
            self._state_path.unlink(missing_ok=True)
            return r

        remaining: list[dict[str, Any]] = []
        changed = False
        for rec in records:
            key = rec.get("key")
            prev_url = rec.get("previous_base_url")
            route_url = rec.get("gateway_route_url")
            info = _target_info(data, key)
            if info is None:
                r.notes.append(
                    f"route for key={key!r} no longer in config; dropping state"
                )
                continue
            if info.base_url != route_url:
                remaining.append(rec)
                r.notes.append(
                    f"key={key!r} is now {info.base_url!r} (not the route we "
                    f"set); leaving alone"
                )
                continue
            _apply_patch(data, key, prev_url)
            changed = True
            loc = ("model.base_url" if key == _PRIMARY_KEY
                   else f"providers.{key}.model.base_url")
            r.notes.append(f"restored {loc} → {prev_url}")

        if changed:
            _atomic_write_yaml(self._config_path, data)
            r.changed_files.append(self._config_path)

        if remaining:
            _write_state_file(self._state_path, remaining)
        else:
            self._state_path.unlink(missing_ok=True)
        return r

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> InstallResult:
        r = InstallResult(agent=self.name, action="status")

        if not self._config_path.exists():
            r.notes.append(f"{self._config_path} does not exist")
            return r

        data = self._read_config()
        state_by_key = {s["key"]: s for s in _read_state_file(self._state_path)
                         if "key" in s}

        if data is None:
            r.notes.append("config.yaml is not readable")
            return r

        keys = _all_target_keys(data)
        if not keys:
            r.notes.append("no model.base_url found")
            return r

        any_routed = False
        for k in keys:
            info = _target_info(data, k)
            if info is None:
                continue
            route_url = self._telos_route_url(info.provider_id)
            loc = ("model" if k == _PRIMARY_KEY else f"providers.{k}.model")
            if info.base_url == route_url:
                any_routed = True
                prev = state_by_key.get(k, {}).get("previous_base_url")
                suffix = f" (restores to {prev!r} on uninstall)" if prev else ""
                r.notes.append(f"  ✓ {loc}: routed → {route_url}{suffix}")
            elif _looks_like_telos_route(info.base_url):
                r.notes.append(
                    f"  ! {loc}: stale telos route ({info.base_url}); "
                    f"re-run install to re-align"
                )
            else:
                r.notes.append(f"  · {loc}: direct → {info.base_url}")
        r.already_installed = any_routed
        return r
