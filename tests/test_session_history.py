"""Reading past conversations back off disk, for all three backends.

The transcripts here are trimmed copies of what Claude Code, Codex and FreeBuff
actually write, including the context each of them injects into the user side
of the conversation — which is exactly what must never end up as a title.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import session_history
from session_history import (
    HistoryEntry,
    clean_user_text,
    describe_age,
    list_history,
    load_turns,
    make_title,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every history store at a throwaway home directory."""
    monkeypatch.setattr(session_history, "_home", lambda: tmp_path)
    return tmp_path


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


# ----- Claude Code -----


def _claude_user(text: str, **extra) -> dict:
    record = {"type": "user", "message": {"role": "user", "content": text}}
    record.update(extra)
    return record


def _claude_assistant(text: str, **extra) -> dict:
    record = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    record.update(extra)
    return record


def _write_claude(home: Path, cwd: str, session_id: str, records: list[dict]) -> Path:
    slug = session_history.claude_project_slug(cwd)
    path = home / ".claude" / "projects" / slug / f"{session_id}.jsonl"
    _write_jsonl(path, records)
    return path


CLAUDE_CWD = str(Path("C:/work/demo")) if Path("C:/").drive else "/work/demo"


def test_claude_conversation_is_titled_by_its_first_message(home: Path) -> None:
    _write_claude(
        home,
        CLAUDE_CWD,
        "11111111-1111-1111-1111-111111111111",
        [
            {"type": "mode", "mode": "default"},
            _claude_user("Fix the login redirect", cwd=CLAUDE_CWD),
            _claude_assistant("Found it in auth.py."),
            _claude_user("Now add a test", cwd=CLAUDE_CWD),
            _claude_assistant("Added one."),
        ],
    )

    entries = list_history("claude", CLAUDE_CWD)

    assert len(entries) == 1
    assert entries[0].title == "Fix the login redirect"
    assert entries[0].session_id == "11111111-1111-1111-1111-111111111111"
    assert entries[0].cwd == CLAUDE_CWD


def test_claude_injected_records_never_become_the_title(home: Path) -> None:
    """The wrappers a CLI writes are not what the person asked for."""
    _write_claude(
        home,
        CLAUDE_CWD,
        "22222222-2222-2222-2222-222222222222",
        [
            _claude_user("<local-command-caveat>Ignore this</local-command-caveat>", cwd=CLAUDE_CWD),
            _claude_user(
                "<command-name>/clear</command-name>\n<command-args></command-args>",
                cwd=CLAUDE_CWD,
            ),
            _claude_user("Session start hook fired", cwd=CLAUDE_CWD, isMeta=True),
            _claude_user("A subagent's own prompt", cwd=CLAUDE_CWD, isSidechain=True),
            _claude_user("What does markdown_rows do?", cwd=CLAUDE_CWD),
            _claude_assistant("It segments a response into rows."),
        ],
    )

    entries = list_history("claude", CLAUDE_CWD)

    assert [entry.title for entry in entries] == ["What does markdown_rows do?"]
    assert [turn.prompt for turn in load_turns(entries[0])] == ["What does markdown_rows do?"]


def test_claude_turns_pair_prompts_with_answers(home: Path) -> None:
    _write_claude(
        home,
        CLAUDE_CWD,
        "33333333-3333-3333-3333-333333333333",
        [
            _claude_user("First question", cwd=CLAUDE_CWD),
            _claude_assistant("First half."),
            _claude_assistant("Second half."),
            _claude_user("Second question", cwd=CLAUDE_CWD),
            _claude_assistant("Second answer."),
        ],
    )

    turns = load_turns(list_history("claude", CLAUDE_CWD)[0])

    assert [turn.prompt for turn in turns] == ["First question", "Second question"]
    # Two assistant records in one turn are one answer, in order.
    assert turns[0].response == "First half.\n\nSecond half."
    assert turns[1].response == "Second answer."


def test_claude_reasoning_and_tool_blocks_stay_out_of_the_answer(home: Path) -> None:
    _write_claude(
        home,
        CLAUDE_CWD,
        "44444444-4444-4444-4444-444444444444",
        [
            _claude_user("Read the file", cwd=CLAUDE_CWD),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "Let me look."},
                        {"type": "tool_use", "name": "Read", "input": {}},
                        {"type": "text", "text": "It defines two functions."},
                    ],
                },
            },
        ],
    )

    turns = load_turns(list_history("claude", CLAUDE_CWD)[0])

    assert turns[0].response == "It defines two functions."


def test_claude_sessions_in_other_folders_are_left_out(home: Path) -> None:
    other = str(Path(CLAUDE_CWD).parent / "elsewhere")
    _write_claude(home, CLAUDE_CWD, "aaaa", [_claude_user("Here", cwd=CLAUDE_CWD)])
    _write_claude(home, other, "bbbb", [_claude_user("There", cwd=other)])

    assert [entry.title for entry in list_history("claude", CLAUDE_CWD)] == ["Here"]
    assert sorted(entry.title for entry in list_history("claude")) == ["Here", "There"]


def test_claude_session_with_no_question_is_not_offered(home: Path) -> None:
    """Nothing was ever asked, so there is nothing to carry on with."""
    _write_claude(home, CLAUDE_CWD, "cccc", [{"type": "mode", "mode": "default"}])

    assert list_history("claude", CLAUDE_CWD) == []


# ----- Codex -----


def _codex_message(role: str, text: str) -> dict:
    kind = "input_text" if role in ("user", "developer") else "output_text"
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": kind, "text": text}],
        },
    }


def _write_codex(home: Path, cwd: str, session_id: str, records: list[dict]) -> Path:
    path = home / ".codex" / "sessions" / "2026" / "08" / "10" / f"rollout-{session_id}.jsonl"
    meta = {
        "type": "session_meta",
        "payload": {"session_id": session_id, "cwd": cwd},
    }
    _write_jsonl(path, [meta, *records])
    return path


CODEX_CWD = CLAUDE_CWD


def test_codex_skips_its_own_context_block(home: Path) -> None:
    """Codex's first "user" message is a wall of injected context."""
    preamble = (
        "<recommended_plugins>\nAirtable\n</recommended_plugins>\n"
        "# AGENTS.md instructions for C:\\work\\demo\n"
        "<INSTRUCTIONS>\nBe brief.\n</INSTRUCTIONS>\n"
        "<environment_context>\n<cwd>C:\\work\\demo</cwd>\n</environment_context>"
    )
    _write_codex(
        home,
        CODEX_CWD,
        "019fe814-a5ee-7f63-a706-53d5f1a2518b",
        [
            _codex_message("developer", "You are Codex."),
            _codex_message("user", preamble),
            _codex_message("user", "what is the weather in seattle"),
            _codex_message("assistant", "Rain, as usual."),
        ],
    )

    entries = list_history("codex", CODEX_CWD)

    assert [entry.title for entry in entries] == ["what is the weather in seattle"]
    assert entries[0].session_id == "019fe814-a5ee-7f63-a706-53d5f1a2518b"
    turns = load_turns(entries[0])
    assert [(turn.prompt, turn.response) for turn in turns] == [
        ("what is the weather in seattle", "Rain, as usual.")
    ]


def test_codex_developer_messages_are_not_the_person(home: Path) -> None:
    _write_codex(
        home,
        CODEX_CWD,
        "019fe814-0000-0000-0000-000000000000",
        [
            _codex_message("developer", "Follow the house style."),
            _codex_message("user", "Rename the module"),
            _codex_message("assistant", "Renamed."),
        ],
    )

    turns = load_turns(list_history("codex", CODEX_CWD)[0])

    assert [turn.prompt for turn in turns] == ["Rename the module"]


def test_codex_sessions_are_scoped_by_working_directory(home: Path) -> None:
    other = str(Path(CODEX_CWD).parent / "elsewhere")
    _write_codex(home, CODEX_CWD, "aaaa-1", [_codex_message("user", "Here")])
    _write_codex(home, other, "bbbb-1", [_codex_message("user", "There")])

    assert [entry.title for entry in list_history("codex", CODEX_CWD)] == ["Here"]
    assert sorted(entry.title for entry in list_history("codex")) == ["Here", "There"]


# ----- FreeBuff -----


def _write_freebuff(
    home: Path,
    project: str,
    chat_id: str,
    messages: list[dict],
    first_prompt: str | None = None,
) -> Path:
    chat = home / ".config" / "manicode" / "projects" / project / "chats" / chat_id
    chat.mkdir(parents=True, exist_ok=True)
    (chat / "chat-messages.json").write_text(json.dumps(messages), encoding="utf-8")
    if first_prompt is not None:
        (chat / "chat-meta.json").write_text(
            json.dumps({"messageCount": len(messages), "firstPrompt": first_prompt}),
            encoding="utf-8",
        )
    return chat


FREEBUFF_CHAT = [
    {"variant": "ai", "blocks": [{"type": "mode-divider"}]},
    {"variant": "user", "content": "testing with a recipe for honey garlic chicken"},
    {
        "variant": "ai",
        "blocks": [
            {"type": "text", "textType": "reasoning", "content": "They want a recipe."},
            {"type": "tool", "toolName": "read_file"},
            {"type": "text", "content": "Here is the recipe."},
        ],
    },
]


def test_freebuff_title_comes_from_its_own_metadata(home: Path) -> None:
    """FreeBuff records the first prompt itself, so listing costs one small read."""
    _write_freebuff(
        home,
        "demo",
        "2026-08-10T02-00-06.557Z",
        FREEBUFF_CHAT,
        first_prompt="testing with a recipe for honey garlic chicken...",
    )

    entries = list_history("freebuff", str(Path("C:/work/demo") if Path("C:/").drive else "/work/demo"))

    assert len(entries) == 1
    # FreeBuff's stored preview is elided; the title must not be cut twice.
    assert entries[0].title == "testing with a recipe for honey garlic chicken"
    assert entries[0].session_id == "2026-08-10T02-00-06.557Z"


def test_freebuff_falls_back_to_the_messages_themselves(home: Path) -> None:
    _write_freebuff(home, "demo", "chat-without-meta", FREEBUFF_CHAT)

    entries = list_history("freebuff")

    assert [entry.title for entry in entries] == ["testing with a recipe for honey garlic chicken"]


def test_freebuff_reasoning_is_not_part_of_the_answer(home: Path) -> None:
    _write_freebuff(home, "demo", "chat-1", FREEBUFF_CHAT)

    turns = load_turns(list_history("freebuff")[0])

    assert [(turn.prompt, turn.response) for turn in turns] == [
        ("testing with a recipe for honey garlic chicken", "Here is the recipe.")
    ]


# ----- Across backends -----


def test_every_backend_appears_newest_first(home: Path) -> None:
    claude = _write_claude(home, CLAUDE_CWD, "dddd", [_claude_user("Claude one", cwd=CLAUDE_CWD)])
    codex = _write_codex(home, CODEX_CWD, "cccc-1", [_codex_message("user", "Codex one")])
    freebuff = _write_freebuff(home, "demo", "chat-2", FREEBUFF_CHAT, first_prompt="FreeBuff one")

    now = time.time()
    os.utime(claude, (now - 300, now - 300))
    os.utime(codex, (now - 60, now - 60))
    os.utime(freebuff / "chat-messages.json", (now - 900, now - 900))

    entries = list_history()

    assert [entry.title for entry in entries] == ["Codex one", "Claude one", "FreeBuff one"]
    assert [entry.backend for entry in entries] == ["codex", "claude", "freebuff"]


def test_the_list_is_capped(home: Path) -> None:
    for index in range(5):
        _write_claude(home, CLAUDE_CWD, f"session-{index}", [_claude_user(f"Ask {index}", cwd=CLAUDE_CWD)])

    assert len(list_history("claude", CLAUDE_CWD, limit=3)) == 3


def test_a_missing_history_store_is_not_an_error(home: Path) -> None:
    assert list_history() == []
    assert load_turns(HistoryEntry("claude", "nope", "Gone", str(home / "gone.jsonl"), 0.0)) == []


# ----- Titles and ages -----


def test_titles_are_one_clean_line() -> None:
    assert make_title("  Fix\nthe   bug  ") == "Fix the bug"
    # Emoji are stripped: the title is read aloud.
    assert make_title("Ship it \U0001f680 now") == "Ship it now"
    long_title = make_title("word " * 40)
    assert len(long_title) <= session_history.TITLE_LIMIT
    assert long_title.endswith("…")


def test_injected_blocks_are_removed_but_real_words_survive() -> None:
    assert clean_user_text("<system-reminder>hush</system-reminder>") == ""
    assert clean_user_text("Real question\n<system-reminder>hush</system-reminder>") == (
        "Real question"
    )
    # A message that merely mentions a tag is still a real message.
    assert clean_user_text("what does <div> mean") == "what does <div> mean"


def test_ages_are_said_the_way_a_person_would() -> None:
    now = 1_000_000.0
    assert describe_age(now - 5, now) == "just now"
    assert describe_age(now - 60, now) == "1 minute ago"
    assert describe_age(now - 1800, now) == "30 minutes ago"
    assert describe_age(now - 3600, now) == "1 hour ago"
    assert describe_age(now - 7200, now) == "2 hours ago"
    assert describe_age(now - 86400, now) == "yesterday"
    assert describe_age(now - 3 * 86400, now) == "3 days ago"
    assert describe_age(0, now) == "unknown"
