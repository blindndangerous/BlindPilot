"""Past conversations: find them on disk, title them, and read them back.

Every backend BlindPilot drives already keeps its own transcript of each
conversation, and every one of them can be resumed by id — Claude Code with
``--resume``, Codex with the app-server's ``thread/resume``, FreeBuff with
``--continue``. What was missing was a way to *find* one again, which is what
this module provides: one list of past conversations across the three
backends, each titled by its first message, plus a reader that turns any of
them back into prompt-and-response turns the GUI can put in its rows.

Where each backend keeps its history:

* Claude Code — ``~/.claude/projects/<slug>/<session-id>.jsonl``, one JSON
  record per line, where ``<slug>`` is the working directory with every
  non-alphanumeric character replaced by a dash.
* Codex — ``~/.codex/sessions/<year>/<month>/<day>/rollout-<stamp>-<id>.jsonl``,
  one JSON record per line, the first of which carries the session id and the
  working directory.
* FreeBuff — ``~/.config/manicode/projects/<project>/chats/<chat-id>/``, a
  directory per conversation holding ``chat-messages.json`` and a
  ``chat-meta.json`` that already stores the first prompt.

Listing is deliberately cheap: it reads only as far into a transcript as it
takes to find the first real user message, because the newest conversations
have to appear the moment the dialog opens. Reading a conversation back
(:func:`load_turns`) parses the whole file, and only happens for the one the
user actually chose.

Deliberately GUI-agnostic and free of any wx dependency, like
``markdown_rows``, so it can be unit-tested on its own (see
``tests/test_session_history.py``).

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

from agent_backends import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    BACKEND_FREEBUFF,
    BACKEND_IDS,
    normalize_backend,
)
from markdown_rows import strip_noise

# How long a title may be before it is cut. Long enough to tell two similar
# conversations apart when they are read out, short enough that arrowing
# through the list is not a chore.
TITLE_LIMIT = 90

# A conversation's first message is normally in the first few records. This
# caps how far the listing scan reads before giving up on a file, so one
# enormous transcript cannot stall the dialog.
_MAX_HEAD_LINES = 400

# Transcripts this large are not read back into rows. Nothing legitimate comes
# close; the guard is against a corrupt or runaway file freezing the GUI.
_MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class HistoryEntry:
    """One past conversation, as far as the picker needs to know about it.

    ``session_id`` is what the backend is asked to resume. ``path`` is the
    transcript :func:`load_turns` reads back. ``cwd`` is the directory the
    conversation ran in where the backend records it — FreeBuff does not, so
    ``folder`` (which is always populated) is what the picker displays.
    """

    backend: str
    session_id: str
    title: str
    path: str
    modified: float
    cwd: str = ""
    folder: str = ""


@dataclass
class HistoryTurn:
    """One exchange: what was asked, and the answer text it produced."""

    prompt: str = ""
    response: str = ""


# Every CLI stuffs context of its own into the user side of the transcript —
# environment blocks, skills manifests, system reminders, plugin listings,
# slash-command wrappers — all of it wrapped in an XML-ish element. Removing
# whole elements leaves exactly what the person typed, which is what a title
# has to be and what a replayed conversation should show.
_INJECTED_BLOCK = re.compile(r"<([A-Za-z][\w.:-]*)>[\s\S]*?</\1>")

# Injected preamble that is not wrapped in an element of its own. Codex writes
# this heading above the instructions it loaded from AGENTS.md.
_INJECTED_MARKERS = (
    re.compile(r"^#\s*AGENTS\.md instructions.*$", re.IGNORECASE | re.MULTILINE),
)


def _home() -> Path:
    """The home directory the three history stores hang off.

    A function rather than a constant so tests can point the whole module at a
    temporary directory.
    """
    return Path.home()


def _flat(text: str) -> str:
    return " ".join((text or "").split())


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def make_title(text: str, limit: int = TITLE_LIMIT) -> str:
    """Turn a first message into the one line that names the conversation.

    Flattened to a single line and stripped of emoji, because this is read
    aloud: a title is only useful if it is the words the person typed.
    """
    flat = _flat(strip_noise(text or ""))
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def clean_user_text(text: str) -> str:
    """What the person actually typed, with the CLI's own additions removed.

    Returns "" for a message that was entirely injected — a plugin listing, an
    environment block, the wrapper around a slash command — which is how such
    records are recognised and skipped.
    """
    remainder = (text or "").strip()
    if not remainder:
        return ""
    # Repeated because removing an outer element can expose another one that
    # was nested inside it.
    previous = ""
    while previous != remainder:
        previous = remainder
        remainder = _INJECTED_BLOCK.sub(" ", remainder)
    for marker in _INJECTED_MARKERS:
        remainder = marker.sub(" ", remainder)
    return remainder.strip()


def _content_text(content: object, kinds: Sequence[str]) -> str:
    """Pull the plain text out of one message's content.

    Content is either a bare string or a list of typed blocks; only the block
    types named in ``kinds`` contribute, so reasoning, tool calls and tool
    results stay out of the transcript the user reads back.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in kinds:
            continue
        piece = str(block.get("text") or "").strip()
        if piece:
            parts.append(piece)
    return "\n\n".join(parts)


def _iter_jsonl(path: Path, limit: Optional[int] = None) -> Iterator[dict]:
    """Yield the JSON objects in a JSONL transcript, skipping unreadable lines.

    A transcript being written to right now can end in a half-flushed line, and
    a crashed run can leave one in the middle, so a bad line is stepped over
    rather than allowed to lose the whole conversation.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for count, line in enumerate(handle):
                if limit is not None and count >= limit:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _append(existing: str, addition: str) -> str:
    if not existing:
        return addition
    return f"{existing}\n\n{addition}"


def _folder_name(cwd: str) -> str:
    name = os.path.basename(os.path.normpath(cwd or ""))
    return name or cwd


def _same_dir(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# ----- Claude Code -----


def claude_project_slug(cwd: str) -> str:
    """Claude Code's directory name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


def _claude_project_dirs(cwd: Optional[str]) -> List[Path]:
    root = _home() / ".claude" / "projects"
    if cwd:
        exact = root / claude_project_slug(cwd)
        return [exact] if exact.is_dir() else []
    try:
        return sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return []


def _claude_user_text(record: dict) -> str:
    """The typed text of a user record, or "" if it is not one."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return ""
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    return clean_user_text(_content_text(message.get("content"), ("text",)))


def _claude_assistant_text(record: dict) -> str:
    if record.get("type") != "assistant" or record.get("isSidechain"):
        return ""
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    return _content_text(message.get("content"), ("text",))


def _claude_entry(path: Path, fallback_cwd: Optional[str]) -> Optional[HistoryEntry]:
    title = ""
    cwd = ""
    for record in _iter_jsonl(path, _MAX_HEAD_LINES):
        if not cwd and record.get("cwd"):
            cwd = str(record.get("cwd") or "")
        text = _claude_user_text(record)
        if text:
            title = make_title(text)
            break
    if not title:
        # Nothing was ever asked in this session, so there is nothing to resume.
        return None
    cwd = cwd or (fallback_cwd or "")
    return HistoryEntry(
        backend=BACKEND_CLAUDE,
        session_id=path.stem,
        title=title,
        path=str(path),
        modified=_mtime(path),
        cwd=cwd,
        folder=_folder_name(cwd),
    )


def _claude_entries(cwd: Optional[str]) -> List[HistoryEntry]:
    entries: List[HistoryEntry] = []
    for folder in _claude_project_dirs(cwd):
        try:
            paths = list(folder.glob("*.jsonl"))
        except OSError:
            continue
        for path in paths:
            entry = _claude_entry(path, cwd)
            if entry is not None:
                entries.append(entry)
    return entries


def _claude_turns(path: Path) -> List[HistoryTurn]:
    turns: List[HistoryTurn] = []
    for record in _iter_jsonl(path):
        prompt = _claude_user_text(record)
        if prompt:
            turns.append(HistoryTurn(prompt=prompt))
            continue
        answer = _claude_assistant_text(record)
        if not answer:
            continue
        if not turns:
            turns.append(HistoryTurn())
        turns[-1].response = _append(turns[-1].response, answer)
    return turns


# ----- Codex -----


def _codex_message(record: dict) -> tuple[str, str]:
    """(role, text) for a Codex transcript record, or ("", "")."""
    if record.get("type") != "response_item":
        return "", ""
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return "", ""
    role = str(payload.get("role") or "")
    if role not in ("user", "assistant"):
        # "developer" is Codex's own instruction channel, never the person.
        return "", ""
    text = _content_text(payload.get("content"), ("input_text", "output_text", "text"))
    if role == "user":
        text = clean_user_text(text)
    if not text:
        return "", ""
    return role, text


def _codex_entry(path: Path, cwd: Optional[str]) -> Optional[HistoryEntry]:
    session_id = ""
    session_cwd = ""
    title = ""
    for record in _iter_jsonl(path, _MAX_HEAD_LINES):
        if record.get("type") == "session_meta":
            payload = record.get("payload")
            if isinstance(payload, dict):
                session_id = str(payload.get("session_id") or payload.get("id") or "")
                session_cwd = str(payload.get("cwd") or "")
            continue
        role, text = _codex_message(record)
        if role == "user":
            title = make_title(text)
            break
    if not session_id or not title:
        return None
    if cwd and not _same_dir(session_cwd, cwd):
        return None
    return HistoryEntry(
        backend=BACKEND_CODEX,
        session_id=session_id,
        title=title,
        path=str(path),
        modified=_mtime(path),
        cwd=session_cwd,
        folder=_folder_name(session_cwd),
    )


def _codex_entries(cwd: Optional[str]) -> List[HistoryEntry]:
    root = _home() / ".codex" / "sessions"
    try:
        paths = list(root.glob("**/*.jsonl"))
    except OSError:
        return []
    entries: List[HistoryEntry] = []
    for path in paths:
        entry = _codex_entry(path, cwd)
        if entry is not None:
            entries.append(entry)
    return entries


def _codex_turns(path: Path) -> List[HistoryTurn]:
    turns: List[HistoryTurn] = []
    for record in _iter_jsonl(path):
        role, text = _codex_message(record)
        if role == "user":
            turns.append(HistoryTurn(prompt=text))
        elif role == "assistant":
            if not turns:
                turns.append(HistoryTurn())
            turns[-1].response = _append(turns[-1].response, text)
    return turns


# ----- FreeBuff -----


def _git_root_name(cwd: str) -> str:
    """The name FreeBuff files a directory's chats under.

    FreeBuff buckets a conversation by the *repository* it was started in, not
    by the directory given to ``--cwd``, so a session started in a subfolder is
    filed under the repository's name.
    """
    try:
        current = Path(cwd).resolve()
    except (OSError, ValueError):
        return ""
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate.name
    return ""


def _freebuff_chat_buckets(cwd: Optional[str]) -> List[Path]:
    root = _home() / ".config" / "manicode" / "projects"
    if cwd:
        names = [name for name in (Path(cwd).name, _git_root_name(cwd)) if name]
        buckets = [root / name / "chats" for name in names]
        buckets.append(root / "chats")
        return [path for path in buckets if path.is_dir()]
    try:
        return sorted(path for path in root.glob("*/chats") if path.is_dir())
    except OSError:
        return []


def _freebuff_messages(chat: Path) -> List[dict]:
    try:
        payload = json.loads((chat / "chat-messages.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _freebuff_first_prompt(chat: Path) -> str:
    """The chat's first typed message, from its metadata where possible.

    FreeBuff writes the first prompt into ``chat-meta.json`` itself, so the
    common case costs one small read instead of parsing the whole conversation.
    """
    try:
        meta = json.loads((chat / "chat-meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if isinstance(meta, dict):
        first = str(meta.get("firstPrompt") or "").strip()
        if first:
            # FreeBuff's own preview is already elided; drop its ellipsis so the
            # title is not cut twice.
            return first[:-3].rstrip() if first.endswith("...") else first
    for message in _freebuff_messages(chat):
        if message.get("variant") == "user":
            text = str(message.get("content") or "").strip()
            if text:
                return text
    return ""


def _freebuff_entry(chat: Path, bucket_folder: str) -> Optional[HistoryEntry]:
    first = _freebuff_first_prompt(chat)
    if not first:
        return None
    return HistoryEntry(
        backend=BACKEND_FREEBUFF,
        session_id=chat.name,
        title=make_title(first),
        path=str(chat),
        modified=_mtime(chat / "chat-messages.json") or _mtime(chat),
        cwd="",
        folder=bucket_folder,
    )


def _freebuff_entries(cwd: Optional[str]) -> List[HistoryEntry]:
    entries: List[HistoryEntry] = []
    seen: set[str] = set()
    for bucket in _freebuff_chat_buckets(cwd):
        # ".../projects/<project>/chats" — the project is what to display.
        bucket_folder = bucket.parent.name if bucket.parent.name != "manicode" else ""
        try:
            chats = list(bucket.iterdir())
        except OSError:
            continue
        for chat in chats:
            if not chat.is_dir() or chat.name in seen:
                continue
            entry = _freebuff_entry(chat, bucket_folder)
            if entry is not None:
                seen.add(chat.name)
                entries.append(entry)
    return entries


def _freebuff_turns(chat: Path) -> List[HistoryTurn]:
    turns: List[HistoryTurn] = []
    for message in _freebuff_messages(chat):
        variant = message.get("variant")
        if variant == "user":
            text = str(message.get("content") or "").strip()
            if text:
                turns.append(HistoryTurn(prompt=text))
            continue
        if variant != "ai":
            continue
        blocks = message.get("blocks")
        if not isinstance(blocks, list):
            continue
        answer: List[str] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            # Reasoning is FreeBuff thinking aloud, not its answer.
            if block.get("textType") == "reasoning":
                continue
            content = str(block.get("content") or "").strip()
            if content:
                answer.append(content)
        if not answer:
            continue
        if not turns:
            turns.append(HistoryTurn())
        turns[-1].response = _append(turns[-1].response, "\n\n".join(answer))
    return turns


# ----- Public API -----

_LISTERS = {
    BACKEND_CLAUDE: _claude_entries,
    BACKEND_CODEX: _codex_entries,
    BACKEND_FREEBUFF: _freebuff_entries,
}

_READERS = {
    BACKEND_CLAUDE: _claude_turns,
    BACKEND_CODEX: _codex_turns,
    BACKEND_FREEBUFF: _freebuff_turns,
}


def list_history(
    backend: Optional[str] = None,
    cwd: Optional[str] = None,
    limit: int = 300,
) -> List[HistoryEntry]:
    """Past conversations, newest first.

    ``backend`` limits the list to one provider; ``None`` returns all three.
    ``cwd`` limits it to conversations that ran in that directory; ``None``
    returns every directory. ``limit`` caps how many are returned, after
    sorting, so a machine with years of history still opens the picker fast.
    """
    if backend is None:
        backends = list(BACKEND_IDS)
    else:
        backends = [normalize_backend(backend)]

    entries: List[HistoryEntry] = []
    for name in backends:
        lister = _LISTERS.get(name)
        if lister is None:
            continue
        try:
            entries.extend(lister(cwd))
        except OSError:
            continue
    entries.sort(key=lambda entry: entry.modified, reverse=True)
    return entries[: max(0, limit)]


def load_turns(entry: HistoryEntry) -> List[HistoryTurn]:
    """Read one past conversation back as prompt-and-response turns.

    Turns with neither a prompt nor an answer are dropped, so an aborted run
    does not leave an empty response in the list.
    """
    reader = _READERS.get(normalize_backend(entry.backend))
    if reader is None:
        return []
    path = Path(entry.path)
    try:
        if path.is_file() and path.stat().st_size > _MAX_TRANSCRIPT_BYTES:
            return []
    except OSError:
        return []
    try:
        turns = reader(path)
    except OSError:
        return []
    return [turn for turn in turns if turn.prompt.strip() or turn.response.strip()]


def describe_age(modified: float, now: Optional[float] = None) -> str:
    """When a conversation was last touched, said the way a person would.

    Spoken by a screen reader, so it avoids clock arithmetic the listener would
    have to do: "12 minutes ago" rather than a timestamp, and a real date only
    once the relative form stops being useful.
    """
    if not modified:
        return "unknown"
    now = time.time() if now is None else now
    seconds = now - modified
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return "1 minute ago" if minutes == 1 else f"{minutes} minutes ago"
    hours = int(seconds // 3600)
    if hours < 24:
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    days = int(seconds // 86400)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return time.strftime("%d %B %Y", time.localtime(modified))
