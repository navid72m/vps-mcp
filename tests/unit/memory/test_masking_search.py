"""Masking (T021) and search ranking (T026). Pure."""

from __future__ import annotations

import time

import pytest

from vps_mcp.memory.masking import mask_note
from vps_mcp.memory.notes import Note
from vps_mcp.memory.search import search, snippet

GLOBS = ["*PASSWORD*", "*SECRET*", "*TOKEN*", "*KEY*"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("password=hunter22", "password=***"),
        ("api_token: abcdef123", "api_token: ***"),
        ("postgres://odoo:s3cret@db/x", "postgres://odoo:***@db/x"),
        ("Authorization: Bearer abcdefghijklmnop", "Authorization: Bearer ***"),
        ("use ghp_" + "a" * 36, "use ***"),
        ("github_pat_" + "B" * 30, "***"),
        ("sk-" + "c" * 30, "***"),
        ("xoxb-1234567890-abcdef", "***"),
        ("AKIA" + "A" * 16, "***"),
        ("glpat-" + "d" * 20, "***"),
    ],
)
def test_masks_secrets(text: str, expected: str) -> None:
    out, n = mask_note(text, GLOBS, [])
    assert out == expected and n == 1


def test_pem_block_masked() -> None:
    pem = "before\n-----BEGIN OPENSSH PRIVATE KEY-----\nabc\ndef\n-----END OPENSSH PRIVATE KEY-----\nafter"
    out, n = mask_note(pem, GLOBS, [])
    assert out == "before\n***\nafter" and n == 1


def test_known_secret_strings_scrubbed() -> None:
    out, n = mask_note(
        "key at /home/op/.ssh/id_vps twice /home/op/.ssh/id_vps", [], ["/home/op/.ssh/id_vps"]
    )
    assert "id_vps" not in out and n == 2


def test_hashes_and_uuids_not_masked() -> None:
    text = (
        "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08 "
        "run 123e4567-e89b-12d3-a456-426614174000"
    )
    assert mask_note(text, GLOBS, []) == (text, 0)


def test_count_multiple() -> None:
    out, n = mask_note("password=a1 and secret: b2 and mysql://u:p@h/d", GLOBS, [])
    assert n == 3 and out.count("***") == 3


# ------------------------------------------------------------------------ search


def mk(
    name: str,
    title: str,
    body: str = "",
    tags: list[str] | None = None,
    type: str = "lesson",
    updated: str = "2026-11-01",
) -> Note:
    return Note(name=name, type=type, title=title, body=body, tags=tags or [], updated=updated)


NOTES = [
    mk("stock-account", "stock_account re-upgrade", "run the reupgrade check", ["pipeline"]),
    mk("night-runs", "Rehearsals at night", "operator prefers night", type="preference"),
    mk("disk", "Disk fills during restore", "stock moves table is big; stock " * 3, ["disk"]),
    mk("older", "stock thing", updated="2026-10-01"),
    mk("newer", "stock thing", updated="2026-11-05"),
]


def test_scoring_order() -> None:
    items, total = search(NOTES, "stock-account")
    assert items[0]["name"] == "stock-account" and items[0]["score"] >= 10
    items, total = search(NOTES, "stock")
    names = [i["name"] for i in items]
    assert names.index("newer") < names.index("older")  # tie broken by updated
    assert total == 4


def test_filters_and_limit() -> None:
    assert [i["name"] for i in search(NOTES, type="preference")[0]] == ["night-runs"]
    assert [i["name"] for i in search(NOTES, tag="disk")[0]] == ["disk"]
    assert len(search(NOTES, "stock", limit=2)[0]) == 2
    assert search(NOTES, "nothing-matches")[0] == []


def test_snippet_around_match() -> None:
    s = snippet("x " * 200 + "needle here", ["needle"])
    assert "needle" in s and len(s) <= 161 and s.startswith("…")


def test_search_500_notes_fast() -> None:
    many = [mk(f"n-{i}", f"title {i}", "body text " * 50 + str(i)) for i in range(500)]
    t = time.perf_counter()
    search(many, "body 42", limit=50)
    assert time.perf_counter() - t < 0.2
