"""HTTP 头 harness 检测 + proxy per-client 记忆的单测（无网络）。

复现并验证修复:Claude Code 用 Haiku 发的辅助请求(标题生成 / 话题检测)
没有工具、没有 ``<system-reminder>`` 标签,纯内容检测会误判成 openclaw。
引入 HTTP 头检测 + proxy per-client 记忆后,这些请求也能正确识别为 hermes。
"""

from __future__ import annotations

from telos.proxy.server import ProxyApp
from telos.scripts.telos_anthropic_transport import (
    _detect_harness,
    _detect_harness_from_headers,
    _detect_harness_signal,
)


# Claude Code 主对话请求:有工具 + system-reminder
_MAIN_REQ = {
    "model": "claude-opus-4-7",
    "system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI."}],
    "tools": [{"name": n} for n in ("Bash", "Read", "Edit", "Write", "Grep")],
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "fix it <system-reminder>cwd=/repo</system-reminder>"}]}],
}

# Claude Code 辅助请求:Haiku 标题生成——无工具、无标签,内容上毫无 harness 特征
_AUX_REQ = {
    "model": "claude-haiku-4-5-20251001",
    "system": [{"type": "text", "text": "Summarize this conversation in 5-10 words."}],
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "User asked to fix a login bug."}]}],
}


# ---------------------------------------------------------------------------
# _detect_harness_from_headers
# ---------------------------------------------------------------------------

def test_headers_user_agent_identifies_claude_code() -> None:
    assert _detect_harness_from_headers(
        {"User-Agent": "claude-cli/1.2.3 (external, cli)"}) == "hermes"
    # key 大小写不敏感
    assert _detect_harness_from_headers(
        {"user-agent": "Claude-CLI/9.9"}) == "hermes"
    print("✓ test_headers_user_agent_identifies_claude_code")


def test_headers_x_app_identifies_claude_code() -> None:
    assert _detect_harness_from_headers({"x-app": "cli"}) == "hermes"
    assert _detect_harness_from_headers({"X-App": "CLI"}) == "hermes"
    print("✓ test_headers_x_app_identifies_claude_code")


def test_headers_unknown_returns_none() -> None:
    assert _detect_harness_from_headers({}) is None
    assert _detect_harness_from_headers({"user-agent": "python-httpx/0.27"}) is None
    assert _detect_harness_from_headers({"x-app": "web"}) is None
    print("✓ test_headers_unknown_returns_none")


# ---------------------------------------------------------------------------
# _detect_harness_signal —— 区分确信信号 vs 兜底
# ---------------------------------------------------------------------------

def test_signal_header_classifies_toolless_aux_request() -> None:
    # 关键复现:tool-less 辅助请求,内容检测必然 miss——
    ua = {"user-agent": "claude-cli/1.2.3"}
    assert _detect_harness_signal(_AUX_REQ) is None          # 无头 → 无信号
    assert _detect_harness_signal(_AUX_REQ, ua) == "hermes"  # 有头 → 确信 hermes
    print("✓ test_signal_header_classifies_toolless_aux_request")


def test_signal_content_rules_still_work() -> None:
    # 富内容请求即使没有头也能确信识别
    assert _detect_harness_signal(_MAIN_REQ) == "hermes"
    # 纯 openclaw 形状(无工具无标签无头)→ 无确信信号
    openclaw_req = {"model": "claude-opus-4-7",
                    "system": [{"type": "text", "text": "You are an agent."}],
                    "messages": [{"role": "user", "content": "hi"}]}
    assert _detect_harness_signal(openclaw_req) is None
    print("✓ test_signal_content_rules_still_work")


def test_detect_harness_backcompat() -> None:
    # 单参数调用(SDK transport / replay / 老测试)仍可用,兜底 openclaw
    assert _detect_harness(_MAIN_REQ) == "hermes"
    assert _detect_harness(_AUX_REQ) == "openclaw"           # 无头 → 兜底
    assert _detect_harness(_AUX_REQ, {"x-app": "cli"}) == "hermes"
    print("✓ test_detect_harness_backcompat")


# ---------------------------------------------------------------------------
# proxy per-client 记忆
# ---------------------------------------------------------------------------

class _FakeRequest:
    """只暴露 ``headers`` —— ``_resolve_harness`` 只用到这个。"""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def test_proxy_resolves_aux_request_via_header() -> None:
    proxy = ProxyApp()
    ua = {"x-api-key": "k1", "user-agent": "claude-cli/1.2.3"}
    # 辅助请求自带 Claude Code 的 User-Agent → 直接判 hermes
    assert proxy._resolve_harness(_FakeRequest(ua), _AUX_REQ) == "hermes"
    print("✓ test_proxy_resolves_aux_request_via_header")


def test_proxy_client_memory_covers_headerless_aux_request() -> None:
    proxy = ProxyApp()
    # turn 1:主对话,带 Claude Code UA → pin 该 client 为 hermes
    main_hdr = {"x-api-key": "k2", "user-agent": "claude-cli/1.2.3"}
    assert proxy._resolve_harness(_FakeRequest(main_hdr), _MAIN_REQ) == "hermes"
    # turn 2:同一 client(同 x-api-key)发 tool-less 辅助请求,且这次连
    # User-Agent 都没有——靠 per-client 记忆继承,不再误判成 openclaw。
    aux_hdr = {"x-api-key": "k2"}
    assert proxy._resolve_harness(_FakeRequest(aux_hdr), _AUX_REQ) == "hermes"
    print("✓ test_proxy_client_memory_covers_headerless_aux_request")


def test_proxy_unknown_client_falls_back_to_openclaw() -> None:
    proxy = ProxyApp()
    # 没有任何信号、没有记忆的全新 client → 兜底 openclaw
    hdr = {"x-api-key": "k3"}
    assert proxy._resolve_harness(_FakeRequest(hdr), _AUX_REQ) == "openclaw"
    print("✓ test_proxy_unknown_client_falls_back_to_openclaw")


def test_proxy_explicit_override_wins() -> None:
    proxy = ProxyApp(harness_override="claude-code")
    # 显式覆盖最优先,且别名归一化成 canonical 名
    hdr = {"x-api-key": "k4", "user-agent": "python-httpx/0.27"}
    assert proxy._resolve_harness(_FakeRequest(hdr), _AUX_REQ) == "hermes"
    print("✓ test_proxy_explicit_override_wins")


if __name__ == "__main__":
    test_headers_user_agent_identifies_claude_code()
    test_headers_x_app_identifies_claude_code()
    test_headers_unknown_returns_none()
    test_signal_header_classifies_toolless_aux_request()
    test_signal_content_rules_still_work()
    test_detect_harness_backcompat()
    test_proxy_resolves_aux_request_via_header()
    test_proxy_client_memory_covers_headerless_aux_request()
    test_proxy_unknown_client_falls_back_to_openclaw()
    test_proxy_explicit_override_wins()
    print("all harness header-detection tests passed")
