"""Data models for VaultNotes notes and spaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SpaceKind = Literal["plain", "vault"]


@dataclass
class Note:
    id: str
    title: str
    body: str = ""
    modified: str = ""
    created: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert Note to dictionary matching Bridge API specification."""
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "modified": self.modified,
            "created": self.created,
            "tags": list(self.tags),
        }
