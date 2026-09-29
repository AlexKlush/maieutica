from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

LESSONS_DIR = Path(__file__).resolve().parent.parent / "lessons"


class Concept(BaseModel):
    id: str
    title: str
    short: str
    bloom: str = "understand"
    weight: float = 1.0
    core: bool = True
    expectation: str
    key_points: list[str]
    questions: dict[str, str] = Field(default_factory=dict)
    hints: list[str] = Field(default_factory=list)
    source_span: str = ""


class Misconception(BaseModel):
    id: str
    concepts: list[str]
    belief: str
    why_wrong: str
    counterexample: str
    probe: str = ""


class Case(BaseModel):
    id: str
    title: str
    prompt: str
    good_signals: list[str] = Field(default_factory=list)
    bad_signals: list[str] = Field(default_factory=list)


class FlawedExample(BaseModel):
    id: str
    okr: str
    flaws: list[str]


class Lesson(BaseModel):
    id: str
    title: str
    source_text: str
    goal: str
    promise: str = ""
    application_task: str = "Примените материал к этой ситуации: как бы вы действовали и почему?"
    critique_task: str = "Посмотрите на этот пример: что в нём не так с точки зрения материала?"
    concepts: list[Concept]
    misconceptions: list[Misconception] = Field(default_factory=list)
    cases: list[Case] = Field(default_factory=list)
    flawed_examples: list[FlawedExample] = Field(default_factory=list)
    curriculum: list[str] = Field(default_factory=list)

    def model_post_init(self, __context) -> None:
        ids = [c.id for c in self.concepts]
        if not self.curriculum:
            self.curriculum = ids
        self.curriculum = [c for c in self.curriculum if c in ids] + [c for c in ids if c not in self.curriculum]

    def concept(self, cid: str) -> Concept:
        for c in self.concepts:
            if c.id == cid:
                return c
        raise KeyError(cid)

    def misconception(self, mid: str) -> Misconception | None:
        for m in self.misconceptions:
            if m.id == mid:
                return m
        return None

    @property
    def application_id(self) -> str | None:
        for c in self.concepts:
            if c.bloom in ("create", "apply") and c.id == self.curriculum[-1]:
                return c.id
        return None

    def brief(self) -> str:
        lines = [f"Тема: {self.title}", f"Цель занятия: {self.goal}", "Понятия, которые ученик должен освоить:"]
        for c in self.concepts:
            lines.append(f"- [{c.id}] {c.title}: {c.expectation}")
        return "\n".join(lines)

    def misconceptions_brief(self, concept_ids: list[str] | None = None) -> str:
        out = []
        for m in self.misconceptions:
            if concept_ids and not set(m.concepts) & set(concept_ids):
                continue
            out.append(f"- [{m.id}] «{m.belief}» — на самом деле: {m.why_wrong}")
        return "\n".join(out)


def load_lesson(name: str = "okr") -> Lesson:
    return Lesson.model_validate(json.loads((LESSONS_DIR / f"{name}.json").read_text()))
