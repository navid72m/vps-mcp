"""Tests for file system access tools on the laptop (files__*)."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.files import (
    files_delete,
    files_edit,
    files_info,
    files_list,
    files_read,
    files_search,
    files_write,
)
from assistant.providers.scripted import ScriptedProvider
from tests.assistant_fakes import harness, say, use
from tests.unit.pa.test_flows import approve_flow


def test_files_read_nonexistent(tmp_path: Path) -> None:
    res = files_read(str(tmp_path / "does_not_exist.txt"))
    assert not res["ok"]
    assert "not found" in res["error"].lower()


def test_files_read_directory(tmp_path: Path) -> None:
    res = files_read(str(tmp_path))
    assert not res["ok"]
    assert "directory" in res["error"].lower()


def test_files_read_and_slice(tmp_path: Path) -> None:
    f = tmp_path / "sample.txt"
    f.write_text("line 1\nline 2\nline 3\nline 4\nline 5\n", encoding="utf-8")

    res = files_read(str(f), start_line=2, end_line=4)
    assert res["ok"]
    assert res["total_lines"] == 5
    assert res["start_line"] == 2
    assert res["end_line"] == 4
    assert "2 | line 2" in res["content"]
    assert "4 | line 4" in res["content"]
    assert "1 | line 1" not in res["content"]


def test_files_read_binary(tmp_path: Path) -> None:
    b = tmp_path / "binary.bin"
    b.write_bytes(bytes([0, 159, 255, 128, 0, 1]))
    res = files_read(str(b))
    assert res["ok"]
    assert res.get("is_binary") is True


def test_files_write_and_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "note.txt"
    res1 = files_write(str(target), "first content")
    assert res1["ok"]
    assert target.read_text(encoding="utf-8") == "first content"

    # Overwrite=False fails
    res2 = files_write(str(target), "new content", overwrite=False)
    assert not res2["ok"]
    assert "already exists" in res2["error"]
    assert target.read_text(encoding="utf-8") == "first content"

    # Overwrite=True succeeds
    res3 = files_write(str(target), "new content", overwrite=True)
    assert res3["ok"]
    assert target.read_text(encoding="utf-8") == "new content"


def test_files_edit(tmp_path: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text("def hello():\n    return 'world'\n", encoding="utf-8")

    # Target not found
    res1 = files_edit(str(f), "missing_target", "replacement")
    assert not res1["ok"]
    assert "not found" in res1["error"]

    # Replacement works
    res2 = files_edit(str(f), "'world'", "'laptop'")
    assert res2["ok"]
    assert res2["replacements"] == 1
    assert f.read_text(encoding="utf-8") == "def hello():\n    return 'laptop'\n"


def test_files_list(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.py").write_text("print(1)")
    (tmp_path / ".hidden").write_text("secret")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("deep")

    # Level 1 list, hidden excluded
    res = files_list(str(tmp_path), max_depth=1, include_hidden=False)
    assert res["ok"]
    names = {e["name"] for e in res["entries"]}
    assert "a.txt" in names
    assert "b.py" in names
    assert "sub" in names
    assert ".hidden" not in names
    assert "c.txt" not in names  # max_depth=1

    # Include hidden
    res_hidden = files_list(str(tmp_path), max_depth=1, include_hidden=True)
    assert any(e["name"] == ".hidden" for e in res_hidden["entries"])

    # Pattern filter
    res_pattern = files_list(str(tmp_path), pattern="*.py")
    assert len(res_pattern["entries"]) == 1
    assert res_pattern["entries"][0]["name"] == "b.py"


def test_files_search(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("the quick brown fox jumps")
    (tmp_path / "two.py").write_text("def find_me(): pass")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "index").write_text("the quick brown fox in git")

    # Name search
    res1 = files_search(str(tmp_path), pattern="*.py")
    assert res1["ok"]
    assert len(res1["matches"]) == 1
    assert "two.py" in res1["matches"][0]["path"]

    # Content grep search (should ignore .git)
    res2 = files_search(str(tmp_path), query="quick brown")
    assert res2["ok"]
    assert len(res2["matches"]) == 1
    assert "one.txt" in res2["matches"][0]["path"]
    assert "jumps" in res2["matches"][0].get("snippet", "")


def test_files_info(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("content here")

    info = files_info(str(f))
    assert info["ok"]
    assert info["exists"] is True
    assert info["is_file"] is True
    assert info["size_bytes"] == len("content here")


def test_files_delete(tmp_path: Path) -> None:
    f = tmp_path / "delete_me.txt"
    f.write_text("bye")

    res = files_delete(str(f))
    assert res["ok"]
    assert not f.exists()

    # Non-empty dir
    d = tmp_path / "dir_with_file"
    d.mkdir()
    (d / "keep.txt").write_text("x")
    res_dir = files_delete(str(d))
    assert not res_dir["ok"]
    assert "not empty" in res_dir["error"].lower()


@pytest.mark.asyncio
async def test_assistant_file_tools_execution(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    p = ScriptedProvider(
        [
            use("files__write", {"path": str(target), "content": "brand new"}),
            say("File written."),
            use("files__read", {"path": str(target)}),
            say("File read: brand new"),
        ]
    )

    h = await harness(tmp_path, p)
    await h.say("Write new.txt please")
    await approve_flow(h)
    await h.finish()

    ask = next(t for t in h.tg.texts() if t.startswith("🔐"))
    assert "create" in ask and "brand new" in ask
    assert target.read_text(encoding="utf-8") == "brand new"
    assert h.tg.texts()[-1] == "File written."

    await h.say("Now read new.txt please")
    await h.finish()
    assert h.tg.texts()[-1] == "File read: brand new"


@pytest.mark.asyncio
async def test_commands_cat_and_ls(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("secret note content")

    p = ScriptedProvider([say("hi")])
    h = await harness(tmp_path, p)

    await h.say(f"/cat {note}")
    assert "secret note content" in h.tg.texts()[-1]

    await h.say(f"/ls {tmp_path}")
    assert "note.txt" in h.tg.texts()[-1]


@pytest.mark.asyncio
async def test_files_send_and_command(tmp_path: Path) -> None:
    doc = tmp_path / "book.pdf"
    doc.write_bytes(b"%PDF-1.4 dummy content")

    p = ScriptedProvider(
        [
            use("files__send", {"path": str(doc), "caption": "Here is your book"}),
            say("Sent book."),
        ]
    )
    h = await harness(tmp_path, p)
    await h.say("Send book please")
    await approve_flow(h)
    await h.finish()
    assert "sendDocument" in h.tg.calls
    assert h.tg.texts()[-1] == "Sent book."

    # Test /send command
    await h.say(f"/send {doc}")
    assert any("Uploading" in t for t in h.tg.texts())


@pytest.mark.asyncio
async def test_rejected_or_unanswered_changes_do_nothing(tmp_path: Path) -> None:
    keep = tmp_path / "keep.txt"
    keep.write_text("original")
    p = ScriptedProvider(
        [
            use("files__delete", {"path": str(keep)}),
            say("Not deleted."),
            use("files__edit", {"path": str(keep), "target": "original", "replacement": "x"}),
            say("Not edited."),
        ]
    )
    h = await harness(tmp_path, p)
    await h.say("delete keep.txt")
    await approve_flow(h, "r")
    await h.finish()
    assert keep.read_text() == "original"
    ask = next(t for t in h.tg.texts() if t.startswith("🔐"))
    assert "DELETE file" in ask and str(keep) in ask
    await h.say("then edit it")
    await approve_flow(h, "r")
    await h.finish()
    assert keep.read_text() == "original"


@pytest.mark.parametrize(
    "rel",
    [
        ".ssh/id_ed25519",
        ".ssh/config",
        ".config/assistant/assistant.toml",
        ".config/vps-mcp/server.toml",
        ".local/share/agent-memory/memories/x.md",
        "projects/app/.env",
        "projects/app/server.pem",
        ".aws/credentials",
    ],
)
def test_protected_paths_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("SECRET")
    for res in (
        files_read(str(f)),
        files_info(str(f)),
        files_write(str(f), "x", overwrite=True),
        files_edit(str(f), "SECRET", "x"),
        files_delete(str(f)),
    ):
        assert not res["ok"] and res["error"].startswith("refused:")
    assert f.read_text() == "SECRET"


def test_listing_and_search_skip_protected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "todo.txt").write_text("call the bank SECRETWORD")
    (tmp_path / "notes" / ".env").write_text("TOKEN=SECRETWORD")
    (tmp_path / "notes" / "deploy.key").write_text("SECRETWORD")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("SECRETWORD")
    listed = files_list(str(tmp_path), max_depth=3, include_hidden=True)
    names = {e["name"] for e in listed["entries"]}
    assert "todo.txt" in names and not names & {".env", "deploy.key", ".ssh", "id_rsa"}
    found = files_search(str(tmp_path), "SECRETWORD")
    assert [m["path"] for m in found["matches"]] == [str(tmp_path / "notes" / "todo.txt")]
    assert files_list(str(tmp_path / ".ssh"))["error"].startswith("refused:")


@pytest.mark.asyncio
async def test_protected_change_is_refused_without_asking(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [use("files__send", {"path": "~/.ssh/id_ed25519"}), say("I can't send that.")]
    )
    h = await harness(tmp_path, p)
    await h.say("send me my ssh key")
    await h.finish()
    assert not any(t.startswith("🔐") for t in h.tg.texts())  # nothing to approve
    assert "sendDocument" not in h.tg.calls
