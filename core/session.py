from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from context.manager import ContextManager


class SessionStore:
    """File-backed persistence for agent sessions.

    A session captures the ContextManager's conversation history and runtime
    state, including revise_history, so a later run can resume exactly where a
    previous one left off.
    """

    def __init__(self, sessions_dir: str | Path) -> None:
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def generate_id() -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{stamp}_{secrets.token_hex(2)}"

    def path_for(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.json"

    def save(self, session_id: str, context_manager: ContextManager) -> Path:
        path = self.path_for(session_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        created_at = now_iso
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                created_at = existing.get("created_at", now_iso)
            except json.JSONDecodeError:
                pass
        payload = {
            "session_id": session_id,
            "created_at": created_at,
            "updated_at": now_iso,
            "history": context_manager.get_history(),
            "state": context_manager.state,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load(self, session_id: str, context_manager: ContextManager) -> dict[str, Any]:
        path = self.path_for(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))

        context_manager.clear()
        for message in data.get("history") or []:
            role = message.get("role")
            content = message.get("content")
            if role is None or content is None:
                continue
            context_manager.add_message(role, content)
        for key, value in (data.get("state") or {}).items():
            context_manager.set_state(key, value)
        return data

    def list_sessions(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in sorted(self.sessions_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            state = data.get("state") or {}
            results.append(
                {
                    "session_id": data.get("session_id", path.stem),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                    "has_spec_summary": state.get("last_spec_summary") is not None,
                    "has_verilog_template": state.get("last_verilog_template") is not None,
                    "has_rtl_review": state.get("last_rtl_review") is not None,
                    "rtl_revise_count": state.get("rtl_revise_count") or 0,
                    "revise_history_len": len(state.get("revise_history") or []),
                    "last_skill": state.get("last_skill"),
                }
            )
        return results
