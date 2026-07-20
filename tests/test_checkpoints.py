"""Tests for checkpoints + scoped rewind (M35)."""

from langchain_core.messages import AIMessage, HumanMessage

from talos import checkpoints as ck


def _msgs(n):
    out = []
    for i in range(n):
        out.append(HumanMessage(content=f"q{i}"))
        out.append(AIMessage(content=f"a{i}"))
    return out


def test_checkpoint_save_and_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ck.save_checkpoint(1, "first task", _msgs(1), snapshot=False)
    ck.save_checkpoint(2, "second task", _msgs(2), snapshot=False)
    cks = ck.list_checkpoints()
    assert len(cks) == 2
    assert cks[-1].label == "second task" and cks[-1].turn == 2


def test_restore_chat_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ck.save_checkpoint(1, "t", _msgs(3), snapshot=False)
    cid = ck.list_checkpoints()[0].id
    messages, files = ck.restore(cid, scope="chat")
    assert messages is not None and len(messages) == 6
    assert files is False                    # chat-only never touches files


def test_files_snapshot_and_rollback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "app.py"
    target.write_text("v1", encoding="utf-8")
    cid = ck.save_checkpoint(1, "v1 state", _msgs(1), snapshot=True)

    if ck.list_checkpoints()[0].tree is None:
        import pytest
        pytest.skip("git not available in this environment")

    target.write_text("v2 broken", encoding="utf-8")
    messages, files = ck.restore(cid, scope="files")
    assert files is True
    assert target.read_text(encoding="utf-8") == "v1"   # rolled back
    assert messages is None                              # files-only keeps chat
