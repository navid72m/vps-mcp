"""SC-002: the needed toolset is prefetched for >= 80% of 20 specialised questions."""

from __future__ import annotations

from tests.unit.pa.test_toolsets import catalog

QUESTIONS = {
    "gitlab": [
        "why did the last pipeline fail on the addons repo?",
        "show me the open merge requests",
        "what changed in the latest commits on main?",
        "read the README file in the deploy repository",
    ],
    "db": [
        "compare the modules in the two databases",
        "which tables are biggest in the production database?",
        "is the sale module installed on the staging db?",
        "give me an overview of the postgres databases",
    ],
    "deploy": [
        "roll back the last deploy",
        "deploy tag v1.5 to staging",
        "which version is deployed right now and where can I deploy to?",
        "update the odoo service to the new release",
    ],
    "migration": [
        "how did last night's migration rehearsal go?",
        "start a rehearsal of the upgrade",
        "show me the latest migration report",
        "run the pre-migration checks",
    ],
    "files": [
        "find the pdf I downloaded yesterday",
        "what's in my Documents folder?",
        "read the file ~/notes/todo.txt",
        "send me the invoice document from my laptop",
    ],
}
CHITCHAT = ["thanks!", "good morning", "what time is it", "how are you?", "ok, nice"]


def test_prefetch_hit_rate() -> None:
    c = catalog()
    misses = []
    for expected, questions in QUESTIONS.items():
        for q in questions:
            picked = [p[0] for p in c.prefetch(q, [], 2.0, 2)]
            if expected not in picked:
                misses.append((q, expected, picked))
    hits = 20 - len(misses)
    assert hits >= 16, f"{hits}/20 hits; misses: {misses}"


def test_chitchat_loads_nothing() -> None:
    c = catalog()
    assert {q: c.prefetch(q, [], 2.0, 2) for q in CHITCHAT} == {q: [] for q in CHITCHAT}
