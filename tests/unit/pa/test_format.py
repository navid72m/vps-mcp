"""Model Markdown rendered for Telegram (HTML parse mode), tables fitted to a phone."""

from __future__ import annotations

from pathlib import Path

from assistant.format import PHONE_COLS, plain, to_html
from assistant.providers.scripted import ScriptedProvider
from tests.assistant_fakes import harness, say

ANSWER = """## Server status

Your server is **healthy**. Details:

| Metric | Value |
|---|---|
| CPU | 4% |
| Disk | **61%** |

* `odoo`: running
- `postgres`: running

| Service | State | Since | Memory | Restarts |
|:--|:-:|--:|---|---|
| odoo_web | running | 3 days | 1.2 GB | 0 |
| postgres | running | 12 days | 800 MB | 1 |

See [the docs](https://example.com/a?b=1&c=2) and run:

```bash
systemctl status odoo <service>
```
"""


def test_markdown_becomes_telegram_html() -> None:
    out = to_html(ANSWER)
    assert out.startswith("<b>Server status</b>")
    assert "Your server is <b>healthy</b>." in out
    assert "• <code>odoo</code>: running" in out and "• <code>postgres</code>: running" in out
    assert '<a href="https://example.com/a?b=1&amp;c=2">the docs</a>' in out
    assert "<pre>systemctl status odoo &lt;service&gt;</pre>" in out
    assert "|" not in out and "**" not in out and "```" not in out and "##" not in out


def test_narrow_table_is_aligned_monospace() -> None:
    out = to_html(ANSWER)
    pre = out[out.index("<pre>Metric") : out.index("</pre>") + 6]
    lines = pre.removeprefix("<pre>").removesuffix("</pre>").split("\n")
    assert lines == ["Metric  Value", "──────  ─────", "CPU     4%", "Disk    61%"]
    assert all(len(line) <= PHONE_COLS for line in lines)


def test_wide_table_becomes_one_bullet_per_row() -> None:
    out = to_html(ANSWER)
    assert "• <b>odoo_web</b>\n   State: running\n   Since: 3 days" in out
    assert "Restarts: 1" in out


def test_two_column_wide_table_is_compact() -> None:
    md = "| Check | Result |\n|---|---|\n| Backup of the main database | finished at 03:12, 4.1 GB |\n"
    assert to_html(md) == "• <b>Backup of the main database</b>: finished at 03:12, 4.1 GB"


def test_escaping_and_identifiers_are_safe() -> None:
    out = to_html("Use <b>x</b> & check my_service_name and *really* 2*3*4")
    assert out.startswith("Use &lt;b&gt;x&lt;/b&gt; &amp; check my_service_name")
    assert "<i>really</i>" in out and "2*3*4" in out


def test_plain_fallback_drops_markers() -> None:
    text = plain(ANSWER)
    assert "Server status" in text and "##" not in text and "**" not in text
    assert "• `odoo`" not in text and "• odoo: running" in text


async def test_answers_are_sent_as_html_and_fall_back_to_plain(tmp_path: Path) -> None:
    p = ScriptedProvider([say("**Up**: all good"), say("**Up** again")])
    h = await harness(tmp_path, p)
    await h.say("status?")
    await h.finish()
    assert h.tg.sent[-1]["parse_mode"] == "HTML" and h.tg.sent[-1]["text"] == "<b>Up</b>: all good"
    h.tg.reject_html = True
    await h.say("again?")
    await h.finish()
    assert "parse_mode" not in h.tg.sent[-1] and h.tg.sent[-1]["text"] == "Up again"
    await h.say("/usage")  # runtime messages stay plain text
    assert "parse_mode" not in h.tg.sent[-1]
