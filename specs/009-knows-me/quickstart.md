# Quickstart: Knows Me

## 1. Automated checks

```bash
uv run pytest -q tests/unit/pa/test_knowme.py tests/unit/pa/test_knowme_flow.py
uv run pytest -q
```

| Expected | Tests |
|---|---|
| The gate saves instantly only from the operator's own message | `test_knowme.py` |
| Corrections supersede, Undo reverts, forget removes | `test_knowme.py` |
| The card stays within budget and never includes sensitive facts | `test_knowme.py` |
| SC-001: 10 facts known in a new conversation, with ≥ 8 answered without a search | `test_knowme_flow.py` |
| SC-003: recaps of 3 conversations in context | `test_knowme_flow.py` |
| SC-006: injected "remember" text in a tool result → no save | `test_knowme_flow.py` |

## 2. Live check on the phone

| Send | Expected |
|---|---|
| "Remember my sister is Sara, she lives in Hamburg" | "📝 Saved …" with ↩️ Undo, in the same reply |
| "Actually she moved to Munich" | "📝 Updated: … (was Hamburg)" |
| `/new`, then "where does my sister live?" | "Munich", with no `memory_search` in `assistant audit` |
| `/me` | Card with Identity / People …, plus Edit and Remove buttons |
| "Forget my sister's city" → tap 🗑 | It's gone; "where does my sister live?" → unknown |
| Talk about something, `/done`, `/new`, then "what did we talk about?" | The recap is mentioned |
| `/interview` | One question at a time; "skip" moves on |
| `/me export` | A Markdown file arrives |

`assistant audit --since 10m` shows a `memory_save` line for each save, update, undo and forget, with no extra `model_call` lines beyond one per conversation end.
