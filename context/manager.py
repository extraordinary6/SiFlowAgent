from __future__ import annotations

from typing import Any


class ContextManager:
    def __init__(self) -> None:
        self.history: list[dict[str, Any]] = []
        self.state: dict[str, Any] = {}

    def add_message(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})

    def get_history(self) -> list[dict[str, Any]]:
        return list(self.history)

    def get_messages_for_llm(self) -> list[dict[str, str]]:
        return [
            {"role": message["role"], "content": str(message["content"])}
            for message in self.history
            if message.get("role") != "system"
        ]

    def set_state(self, key: str, value: Any) -> None:
        self.state[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    def clear(self) -> None:
        self.history.clear()
        self.state.clear()
