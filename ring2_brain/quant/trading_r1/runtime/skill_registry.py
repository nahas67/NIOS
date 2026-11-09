"""Skill registry surface used by the prompt assembler."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["SkillRegistry", "SkillSpec"]


@dataclass(frozen=True, slots=True)
class SkillSpec:
    name: str
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


class SkillRegistry:
    def __init__(self, skills: list[SkillSpec] | None = None) -> None:
        self._skills = {skill.name: skill for skill in (skills or [])}

    def register(self, skill: SkillSpec) -> SkillSpec:
        self._skills[skill.name] = skill
        return skill

    def get(self, name: str) -> SkillSpec | None:
        return self._skills.get(name)

    def all(self) -> tuple[SkillSpec, ...]:
        return tuple(self._skills.values())
