from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class SkillMetadata(BaseModel):
    name: str = Field(..., description="Unique skill name")
    description: str = Field(..., description="Human-readable skill description")


class BaseSkill(ABC):
    metadata: SkillMetadata

    def __init__(self, name: str, description: str) -> None:
        self.metadata = SkillMetadata(name=name, description=description)

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        raise NotImplementedError
