"""Persistent, isolated ChatGPT sessions built on jAIgent's session store."""

from __future__ import annotations

import json
import re
import threading
from typing import TYPE_CHECKING, Any

from jaigent.errors import ToolError
from jaigent.session import Session, list_sessions, load
from jaigent.tools import Tool

if TYPE_CHECKING:
    from jaigent.chatgpt import GatewayClient

CHATGPT_SESSION_SOURCE = "chatgpt"
# Accept the shorter IDs from early 0.5.7 previews while new sessions use a
# 32-bit random suffix; the timestamp keeps them sortable and collisions are checked.
_SESSION_ID_RE = re.compile(r"^\d{8}-\d{6}-(?:[0-9a-f]{4}|[0-9a-f]{8})$")


class ChatGPTSessionStore:
    """Manage persistent web sessions without exposing unrelated CLI chats."""

    def __init__(
        self,
        gateway: GatewayClient,
        *,
        workspace: str,
        max_messages: int,
        max_message_chars: int,
    ) -> None:
        self.gateway = gateway
        self.workspace = workspace
        self.max_messages = max_messages
        self.max_message_chars = max_message_chars
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def list(self, limit: Any = 20) -> str:
        """List metadata for ChatGPT-created sessions, never their transcripts."""
        if limit is None:
            limit = 20
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ToolError("`limit` must be an integer between 1 and 100.")
        rows = [
            {
                "session_id": item.id,
                "title": item.title or "untitled",
                "model": item.model or "auto",
                "turns": item.turns,
                "updated": item.updated,
            }
            for item in list_sessions(limit=limit, source=CHATGPT_SESSION_SOURCE)
            if _SESSION_ID_RE.fullmatch(item.id)
        ]
        return json.dumps({"sessions": rows}, ensure_ascii=False)

    def start(self, title: Any = "", model: Any = "auto") -> str:
        """Create and persist an independent session with a collision-safe ID."""
        if title is None:
            title = ""
        if not isinstance(title, str) or len(title) > 200:
            raise ToolError("`title` must be text of at most 200 characters.")
        resolved_model = _model_name(model)
        session = Session.new(
            model=resolved_model,
            workspace=self.workspace,
            source=CHATGPT_SESSION_SOURCE,
        )
        if title.strip():
            session.set_title_from(title)
        session.save()
        return json.dumps(
            {
                "session_id": session.id,
                "title": session.title or "untitled",
                "model": session.model,
                "created": session.created,
            },
            ensure_ascii=False,
        )

    def chat(self, session_id: Any, message: Any, model: Any = None) -> str:
        """Continue one session through the existing OpenAI-compatible gateway."""
        if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
            raise ToolError("`session_id` is not a valid jAIgent session id.")
        if not isinstance(message, str) or not message.strip():
            raise ToolError("`message` must be non-empty text.")
        if len(message) > self.max_message_chars:
            raise ToolError(f"`message` must be at most {self.max_message_chars:,} characters.")
        session_lock = self._lock_for(session_id)
        with session_lock:
            session = self._load_chatgpt_session(session_id)
            resolved_model = _model_name(model if model is not None else session.model or "auto")
            messages = [{"role": role, "content": text} for role, text in session.transcript()]
            if len(messages) >= self.max_messages:
                raise ToolError(
                    f"This session has reached the {self.max_messages}-message context limit. "
                    "Start a new session to continue."
                )
            messages.append({"role": "user", "content": message})
            answer = self.gateway.chat(messages, resolved_model)

            session.set_title_from(message)
            session.model = resolved_model
            session.messages.extend(
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": answer},
                ]
            )
            session.touch(session.messages)
            session.save()
        return json.dumps(
            {
                "session_id": session.id,
                "answer": answer,
                "model": resolved_model,
                "turns": session.turns,
                "updated": session.updated,
            },
            ensure_ascii=False,
        )

    def delete(self, session_id: Any) -> str:
        """Delete only a ChatGPT-originated session with a safe generated ID."""
        if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
            raise ToolError("`session_id` is not a valid jAIgent session id.")
        with self._lock_for(session_id):
            session = self._load_chatgpt_session(session_id)
            if not session.delete():
                raise ToolError("That jAIgent ChatGPT session no longer exists.")
        return json.dumps({"deleted": True, "session_id": session_id})

    def _load_chatgpt_session(self, session_id: str) -> Session:
        session = load(session_id)
        if session is None or session.id != session_id or session.source != CHATGPT_SESSION_SOURCE:
            raise ToolError(
                "No ChatGPT session matches that id. List ChatGPT sessions or start a new one."
            )
        return session

    def _lock_for(self, session_id: str) -> threading.RLock:
        """Serialize writes to one session while leaving other sessions concurrent."""
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[session_id] = lock
            return lock


def _model_name(value: Any) -> str:
    if value is None:
        return "auto"
    if not isinstance(value, str) or len(value) > 200:
        raise ToolError("`model` must be a string of at most 200 characters.")
    return value.strip() or "auto"


def build_session_tools(
    gateway: GatewayClient,
    *,
    workspace: str,
    max_messages: int,
    max_message_chars: int,
) -> list[Tool]:
    """Create tools for parallel, persisted ChatGPT sessions.

    Session mutation tools are marked non-read-only. The remote bridge exposes
    them only when both its write opt-in and the gateway's writable policy are
    enabled. The regular jAIgent session store remains the source of truth.
    """
    store = ChatGPTSessionStore(
        gateway,
        workspace=workspace,
        max_messages=max_messages,
        max_message_chars=max_message_chars,
    )
    return [
        Tool(
            name="jaigent_sessions_list",
            description=(
                "List recent sessions created from ChatGPT by id, title and turn count. "
                "This does not reveal CLI session transcripts."
            ),
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                "additionalProperties": False,
            },
            func=lambda limit=20: store.list(limit),
            read_only=True,
        ),
        Tool(
            name="jaigent_session_start",
            description=(
                "Start a separate persistent jAIgent conversation. Keep the returned "
                "session_id and use it with jaigent_session_chat; start another session "
                "whenever you need an independent context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 200},
                    "model": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "jAIgent model id; defaults to auto.",
                    },
                },
                "additionalProperties": False,
            },
            func=lambda title="", model="auto": store.start(title, model),
            read_only=False,
        ),
        Tool(
            name="jaigent_session_chat",
            description=(
                "Send a message to one existing ChatGPT session. Its history is isolated "
                "from other sessions and saved after a successful response. Use the exact "
                "session_id returned by jaigent_session_start or jaigent_sessions_list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "pattern": _SESSION_ID_RE.pattern},
                    "message": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": max_message_chars,
                    },
                    "model": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "Optional jAIgent model override for this turn.",
                    },
                },
                "required": ["session_id", "message"],
                "additionalProperties": False,
            },
            func=lambda session_id, message, model=None: store.chat(session_id, message, model),
            read_only=False,
        ),
        Tool(
            name="jaigent_session_delete",
            description="Delete one ChatGPT-created jAIgent session and its saved transcript.",
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string", "pattern": _SESSION_ID_RE.pattern}},
                "required": ["session_id"],
                "additionalProperties": False,
            },
            func=lambda session_id: store.delete(session_id),
            dangerous=True,
            read_only=False,
        ),
    ]
