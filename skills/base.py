from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class SkillMetadata(BaseModel):
    name: str = Field(..., description="Unique skill name")
    description: str = Field(..., description="Human-readable skill description")
    parameters_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON-schema-like description of skill parameters used by the router",
    )


class BaseSkill(ABC):
    metadata: SkillMetadata

    def __init__(
        self,
        name: str,
        description: str,
        parameters_schema: dict[str, Any] | None = None,
    ) -> None:
        self.metadata = SkillMetadata(
            name=name,
            description=description,
            parameters_schema=parameters_schema or {},
        )

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return self.metadata.parameters_schema

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        raise NotImplementedError
