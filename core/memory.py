from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class WorkingMemory:
    """Turn-scoped scratchpad. Cheap, in-process, cleared aggressively.

    Intended for ephemeral values that live inside a single agent turn: the
    current sub-step index, the raw LLM response being parsed, temporary
    paths, etc. Never persisted to disk. Never serialized into a session.
    """

    def __init__(self) -> None:
        self._scratch: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self._scratch[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._scratch.get(key, default)

    def pop(self, key: str, default: Any = None) -> Any:
        return self._scratch.pop(key, default)

    def clear(self) -> None:
        self._scratch.clear()

    def snapshot(self) -> dict[str, Any]:
        return dict(self._scratch)


class SessionMemory:
    """Run/session-scoped memory: the conversation history plus a flat state dict.

    This is the tier that gets serialized by ``core/session.py`` and replayed
    when a session is loaded. It is what the rest of the codebase used to call
    directly on ``ContextManager``; it is now an explicit layer so ``working``
    and ``long_term`` tiers do not get tangled with it.
    """

    def __init__(self) -> None:
        self.history: list[dict[str, Any]] = []
        self.state: dict[str, Any] = {}

    def add_message(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})

    def set_state(self, key: str, value: Any) -> None:
        self.state[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    def clear(self) -> None:
        self.history.clear()
        self.state.clear()


class LongTermMemory:
    """Persistent cross-session knowledge store, backed by JSON files.

    Organized into namespaces; each namespace lives in its own JSON file under
    ``root``. Two store shapes are supported:

    - dict-valued namespaces (default): keyed records with automatic
      ``updated_at`` stamps; call ``put`` / ``load``.
    - list-valued namespaces: append-only logs; call ``append`` / ``load``.

    The VerifierAgent uses the list-valued ``sim_history`` namespace so that
    every simulator pass/fail is preserved across runs and can be mined later.
    """

    DEFAULT_NAMESPACES: tuple[str, ...] = ("patterns", "lessons", "sim_history")

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def default_for(cls, project_root: str | Path) -> "LongTermMemory":
        return cls(Path(project_root) / "data" / "memory")

    def _path(self, namespace: str) -> Path:
        safe = namespace.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}.json"

    def load(self, namespace: str, default: Any = None) -> Any:
        path = self._path(namespace)
        if not path.exists():
            return {} if default is None else default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {} if default is None else default

    def save(self, namespace: str, data: Any) -> Path:
        path = self._path(namespace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def put(self, namespace: str, key: str, value: dict[str, Any]) -> Path:
        data = self.load(namespace, default={})
        if not isinstance(data, dict):
            raise ValueError(f"namespace {namespace!r} is not a dict-valued store")
        entry = dict(value)
        entry.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        data[key] = entry
        return self.save(namespace, data)

    def append(self, namespace: str, entry: dict[str, Any]) -> Path:
        data = self.load(namespace, default=[])
        if not isinstance(data, list):
            raise ValueError(f"namespace {namespace!r} is not a list-valued store")
        record = dict(entry)
        record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        data.append(record)
        return self.save(namespace, data)

    def list_namespaces(self) -> list[str]:
        return sorted(path.stem for path in self.root.glob("*.json"))

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for namespace in self.list_namespaces():
            data = self.load(namespace)
            if isinstance(data, list):
                out[namespace] = {"type": "list", "count": len(data)}
            elif isinstance(data, dict):
                out[namespace] = {"type": "dict", "keys": len(data)}
            else:
                out[namespace] = {"type": "other"}
        return out

    def recent(self, namespace: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self.load(namespace, default=[])
        if not isinstance(data, list):
            return []
        return list(data[-limit:])

    def filter(
        self,
        namespace: str,
        predicate,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        data = self.load(namespace, default=[])
        if not isinstance(data, list):
            return []
        matched: list[dict[str, Any]] = []
        for entry in data:
            try:
                if predicate(entry):
                    matched.append(entry)
            except Exception:  # noqa: BLE001 - predicate errors shouldn't crash recall
                continue
            if limit is not None and len(matched) >= limit:
                break
        return matched


__all__: Iterable[str] = (
    "WorkingMemory",
    "SessionMemory",
    "LongTermMemory",
)
