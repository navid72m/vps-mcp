"""Front matter (T007) and the notes model (T009). Pure."""

from __future__ import annotations

import pytest

from vps_mcp.errors import VpsMcpError
from vps_mcp.memory import frontmatter
from vps_mcp.memory.frontmatter import FrontMatterError
from vps_mcp.memory.notes import (
    NOTE_TYPES,
    Note,
    links,
    slugify,
    validate_name,
    validate_tags,
    validate_title,
    validate_type,
)

# ------------------------------------------------------------------ front matter


def test_round_trip_scalars_quotes_and_lists() -> None:
    header = {
        "name": "n1",
        "type": "lesson",
        "title": "a: b # not a comment",
        "tags": ["x", "y-z"],
        "custom": "  padded ",
    }
    text = frontmatter.render(header, "body line\n---\nstill body")
    parsed, body = frontmatter.parse(text)
    assert parsed == header
    assert body == "body line\n---\nstill body\n"


def test_key_order_is_stable_and_unknown_keys_kept() -> None:
    text = frontmatter.render({"zeta": "1", "title": "t", "name": "n"}, "")
    assert text.splitlines()[1:4] == ["name: n", "title: t", "zeta: 1"]


def test_crlf_and_bom() -> None:
    parsed, body = frontmatter.parse("﻿---\r\nname: a\r\n---\r\nhello\r\n")
    assert parsed == {"name": "a"} and body.strip() == "hello"


@pytest.mark.parametrize(
    "bad",
    ["name: a\n---\n", "---\nname: a\n", "---\nthis is not a pair\n---\n", ""],
)
def test_malformed_headers_raise(bad: str) -> None:
    with pytest.raises(FrontMatterError):
        frontmatter.parse(bad)


def test_quoted_list_items_and_empty_list() -> None:
    parsed, _ = frontmatter.parse('---\ntags: ["a, b", c]\nother: []\n---\n')
    assert parsed["tags"] == ["a, b", "c"] and parsed["other"] == []


# ------------------------------------------------------------------------- notes


@pytest.mark.parametrize("name", ["a", "stock-account", "x" * 64, "0-day"])
def test_valid_names(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "..", "../x", "a/b", ".hidden", "Upper", "x" * 65, "-lead", "a b", "a\x00", 5]
)
def test_invalid_names(name: object) -> None:
    with pytest.raises(VpsMcpError) as e:
        validate_name(name)
    assert e.value.category.value == "invalid_input"


def test_tags_types_titles() -> None:
    assert validate_tags(["a", "b_c", "a"]) == ["a", "b_c"]
    for bad in (["A"], ["x" * 33], [f"t{i}" for i in range(11)], "notalist"):
        with pytest.raises(VpsMcpError):
            validate_tags(bad)
    assert len(NOTE_TYPES) == 7
    with pytest.raises(VpsMcpError):
        validate_type("rumour")
    with pytest.raises(VpsMcpError):
        validate_title("two\nlines")
    with pytest.raises(VpsMcpError):
        validate_title("x" * 121)
    assert validate_title("  ok  ") == "ok"


def test_slugify() -> None:
    assert slugify("Stock_Account must be re-upgraded!") == "stock-account-must-be-re-upgraded"
    assert slugify("Ünïcödé") == "unicode"
    assert slugify("!!!") == "note"
    assert len(slugify("word " * 40)) <= 64


def test_links_extraction() -> None:
    assert links("see [[a-b]] and [[../../x]] and [[a-b]]") == [("a-b", True), ("../../x", False)]


def test_from_text_defaults_for_hand_written_file() -> None:
    n = Note.from_text("my-note", "---\ntitle: Hello\n---\nBody\n", 1762600000.0)
    assert (n.name, n.type, n.source) == ("my-note", "fact", "operator")
    assert n.created.startswith("2025-11-08") and n.updated == n.created
    assert n.body == "Body"


def test_unknown_type_in_file_is_malformed() -> None:
    with pytest.raises(FrontMatterError):
        Note.from_text("x", "---\ntype: rumour\n---\n", 0)


def test_status_defaults_and_round_trip() -> None:
    issue = Note(name="i", type="issue", title="t")
    assert issue.effective_status == "open"
    assert Note(name="d", type="decision", title="t").effective_status is None
    done = Note(name="i", type="issue", title="t", status="done", extra={"x": "1"})
    again = Note.from_text("i", done.to_text(), 0)
    assert again.status == "done" and again.extra == {"x": "1"}
