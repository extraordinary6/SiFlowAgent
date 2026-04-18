from __future__ import annotations

from pathlib import Path
from typing import Any

from core.memory import LongTermMemory, SessionMemory, WorkingMemory


class ContextManager:
    """Facade over the tiered memory system.

    Back-compat: every public method the rest of the codebase used to call on
    ``ContextManager`` still works and transparently targets the session tier.
    The three explicit tiers are exposed as attributes for skills that want to
    opt-in:

    - ``working``    : turn-scoped scratchpad (ephemeral)
    - ``session``    : run-scoped history + state (serializable)
    - ``long_term``  : cross-session JSON KV store (persistent)

    ``long_term`` is optional so callers that don't care don't pay the disk
    cost. The orchestrator attaches one automatically on startup.
    """

    def __init__(self, long_term_root: str | Path | None = None) -> None:
        self.working = WorkingMemory()
        self.session = SessionMemory()
        self.long_term: LongTermMemory | None = (
            LongTermMemory(long_term_root) if long_term_root else None
        )

    # ---- session-tier proxies (public API preserved) ----

    @property
    def history(self) -> list[dict[str, Any]]:
        return self.session.history

    @property
    def state(self) -> dict[str, Any]:
        return self.session.state

    def add_message(self, role: str, content: str) -> None:
        self.session.add_message(role, content)

    def get_history(self) -> list[dict[str, Any]]:
        return list(self.session.history)

    def get_messages_for_llm(self) -> list[dict[str, str]]:
        return [
            {"role": message["role"], "content": str(message["content"])}
            for message in self.session.history
            if message.get("role") != "system"
        ]

    def set_state(self, key: str, value: Any) -> None:
        self.session.set_state(key, value)

    def get_state(self, key: str, default: Any = None) -> Any:
        return self.session.get_state(key, default)

    def clear(self) -> None:
        """Clear working + session tiers. Long-term memory is never touched."""
        self.working.clear()
        self.session.clear()

    # ---- long-term tier helpers ----

    def attach_long_term(self, long_term: LongTermMemory) -> None:
        self.long_term = long_term

    def memory_summary(self) -> dict[str, Any]:
        return {
            "working_keys": len(self.working.snapshot()),
            "session_messages": len(self.session.history),
            "session_state_keys": len(self.session.state),
            "long_term": self.long_term.summary() if self.long_term else None,
        }
