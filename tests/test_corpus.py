"""``telos.corpus`` unit tests: recording / loading / listing session corpora."""

from __future__ import annotations

import tempfile
from pathlib import Path

from telos.corpus import list_sessions, load_session, record_call


def _sample_request(text: str) -> dict:
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
    }


def test_record_and_load_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        record_call(cd, "sess-1", 1, _sample_request("first"))
        record_call(cd, "sess-1", 2, _sample_request("second"))
        turns = load_session(cd, "sess-1")
        assert len(turns) == 2
        assert turns[0]["call_index"] == 1
        assert turns[1]["request"]["messages"][0]["content"][0]["text"] == "second"
    print("✓ test_record_and_load_roundtrip")


def test_load_session_sorts_by_call_index() -> None:
    """Out-of-order writes should still be read back in ascending call_index order."""
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        record_call(cd, "s", 3, _sample_request("c"))
        record_call(cd, "s", 1, _sample_request("a"))
        record_call(cd, "s", 2, _sample_request("b"))
        turns = load_session(cd, "s")
        assert [t["call_index"] for t in turns] == [1, 2, 3]
    print("✓ test_load_session_sorts_by_call_index")


def test_list_sessions() -> None:
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        record_call(cd, "alpha", 1, _sample_request("x"))
        record_call(cd, "beta", 1, _sample_request("y"))
        record_call(cd, "beta", 2, _sample_request("z"))
        infos = {i.session_id: i for i in list_sessions(cd)}
        assert set(infos) == {"alpha", "beta"}
        assert infos["beta"].n_calls == 2
        assert infos["alpha"].n_calls == 1
    print("✓ test_list_sessions")


def test_load_by_inner_id_when_filename_differs() -> None:
    """session_id with special characters → filename is sanitized; should still load by the real id."""
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        weird = "client/key:abc 123"
        record_call(cd, weird, 1, _sample_request("hi"))
        # the filename is sanitized, but load_session can find it back by the internal session_id
        turns = load_session(cd, weird)
        assert len(turns) == 1
        assert turns[0]["session_id"] == weird
    print("✓ test_load_by_inner_id_when_filename_differs")


def test_load_missing_session_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        try:
            load_session(Path(td), "does-not-exist")
        except FileNotFoundError:
            print("✓ test_load_missing_session_raises")
            return
        raise AssertionError("should raise FileNotFoundError")


def test_empty_corpus_lists_nothing() -> None:
    with tempfile.TemporaryDirectory() as td:
        assert list_sessions(Path(td)) == []
    assert list_sessions(Path("/tmp/telos-corpus-does-not-exist-xyz")) == []
    print("✓ test_empty_corpus_lists_nothing")


def main() -> None:
    test_record_and_load_roundtrip()
    test_load_session_sorts_by_call_index()
    test_list_sessions()
    test_load_by_inner_id_when_filename_differs()
    test_load_missing_session_raises()
    test_empty_corpus_lists_nothing()
    print("\nall corpus tests passed.")


if __name__ == "__main__":
    main()
