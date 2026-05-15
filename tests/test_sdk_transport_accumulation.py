"""SDK transport（Anthropic + OpenAI）的多轮累积回归。

策略：monkey-patch transport 的 ``_inner`` client 成 mock，避免任何网络。
然后做 3 轮调用，验证 ``transport.session_state`` 内部计数器单调递增。
"""

from __future__ import annotations

from typing import Any

from stela.scripts.stela_anthropic_transport import StelaAnthropicTransport
from stela.scripts.stela_transport import StelaOpenAITransport


# ---------------------------------------------------------------------------
# Mock Anthropic 响应（实现 ``response.usage.model_dump()``）
# ---------------------------------------------------------------------------

class _MockAnthropicResponse:
    def __init__(self, cache_creation: int) -> None:
        self._usage = {
            "input_tokens": 50,
            "cache_read_input_tokens": 6500 - cache_creation,
            "cache_creation_input_tokens": cache_creation,
            "output_tokens": 1,
        }
        # 模拟 anthropic.types.Message 的最小子集
        self.id = "msg_x"
        self.role = "assistant"
        self.content = []
        self.usage = _MockUsage(self._usage)


class _MockUsage:
    def __init__(self, d: dict[str, Any]) -> None:
        self._d = d

    def model_dump(self) -> dict[str, Any]:
        return dict(self._d)


class _MockAnthropicMessages:
    def __init__(self) -> None:
        self.call = 0
        self._seq = [5000, 1500, 0]

    def create(self, **kwargs):
        self.call += 1
        return _MockAnthropicResponse(self._seq[min(self.call - 1, len(self._seq) - 1)])


class _MockAnthropicClient:
    def __init__(self) -> None:
        self.messages = _MockAnthropicMessages()


def _make_req() -> dict:
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 64,
        "system": [
            {"type": "text", "text": "You are an engineer agent."},
            # 大文档 → ref-pool
            {"type": "text", "text": "AUTH SPEC:\n" + ("规则细节…\n" * 400)},
        ],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "do a thing"}]},
        ],
    }


def test_anthropic_transport_accumulates() -> None:
    t = StelaAnthropicTransport(api_key="test-not-real",
                                 session_id="multi-anth")
    t._inner = _MockAnthropicClient()  # 拦截网络

    for _ in range(3):
        t.messages.create(**_make_req())

    state = t.session_state
    assert state.stats.cumulative_cache_creation == 5000 + 1500 + 0, \
        f"期望 6500，实际 {state.stats.cumulative_cache_creation}"
    assert state.stats.real_requests_since_refresh == 3, \
        f"期望 3，实际 {state.stats.real_requests_since_refresh}"
    assert state.refpool.slugs, "ref-pool 应当注册了大文档的 slug"
    assert len(state.refpool.slugs) == 1, \
        f"3 轮请求 ref-pool 仅应注册 1 个 slug，实际 {state.refpool.slugs}"
    print(f"✓ test_anthropic_transport_accumulates "
          f"(cache_creation={state.stats.cumulative_cache_creation}, "
          f"requests={state.stats.real_requests_since_refresh}, "
          f"slugs={list(state.refpool.slugs)})")


# ---------------------------------------------------------------------------
# Mock OpenAI (DeepSeek-via-OpenRouter) response
# ---------------------------------------------------------------------------

class _MockOpenAICompletions:
    def __init__(self) -> None:
        self.call = 0

    def create(self, **kwargs):
        self.call += 1

        class _Resp:
            usage = _MockUsage({
                # DeepSeek-style：prompt_cache_hit_tokens / prompt_cache_miss_tokens
                "prompt_cache_hit_tokens": 6500,
                "prompt_cache_miss_tokens": 100,
                "completion_tokens": 50,
            })
            choices = []
        return _Resp()


class _MockOpenAIChat:
    def __init__(self) -> None:
        self.completions = _MockOpenAICompletions()


class _MockOpenAIClient:
    def __init__(self) -> None:
        self.chat = _MockOpenAIChat()


def _make_openai_req() -> dict:
    return {
        "model": "deepseek-chat",
        "max_tokens": 64,
        "messages": [
            {"role": "system", "content": "You are a senior engineer."},
            {"role": "system", "content":
                "<file path=\"spec.md\">" + ("rule…\n" * 400) + "</file>"},
            {"role": "user", "content": "do a thing"},
        ],
        "tools": [
            {"type": "function", "function": {"name": "Bash",
                                              "parameters": {"type": "object"}}},
        ],
    }


def test_openai_transport_accumulates() -> None:
    t = StelaOpenAITransport(api_key="test-not-real",
                              session_id="multi-oai")
    t._inner = _MockOpenAIClient()

    for _ in range(3):
        t.chat.completions.create(**_make_openai_req())

    state = t.session_state
    # DeepSeek 的 usage 没有 cache_creation_input_tokens → cumulative = 0（正确）
    assert state.stats.cumulative_cache_creation == 0
    # 但 R8 请求计数器应当累积
    assert state.stats.real_requests_since_refresh == 3, \
        f"期望 3，实际 {state.stats.real_requests_since_refresh}"
    print(f"✓ test_openai_transport_accumulates "
          f"(requests={state.stats.real_requests_since_refresh}, "
          f"slugs={list(state.refpool.slugs)})")


def test_anthropic_transport_independent_instances() -> None:
    """两个 transport 实例彼此独立 —— state 不串台。"""
    t1 = StelaAnthropicTransport(api_key="k1", session_id="a")
    t2 = StelaAnthropicTransport(api_key="k2", session_id="b")
    t1._inner = _MockAnthropicClient()
    t2._inner = _MockAnthropicClient()

    t1.messages.create(**_make_req())
    t1.messages.create(**_make_req())
    t2.messages.create(**_make_req())

    assert t1.session_state.stats.real_requests_since_refresh == 2
    assert t2.session_state.stats.real_requests_since_refresh == 1
    print("✓ test_anthropic_transport_independent_instances")


def main() -> None:
    test_anthropic_transport_accumulates()
    test_openai_transport_accumulates()
    test_anthropic_transport_independent_instances()
    print("\nall SDK-transport accumulation tests passed.")


if __name__ == "__main__":
    main()
