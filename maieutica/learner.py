from __future__ import annotations

from dataclasses import dataclass, field

# Байесовское отслеживание знаний (BKT) с тремя исходами наблюдения и поправкой на подсказки.
# P(наблюдение | знает) и P(наблюдение | не знает): частично верный ответ несёт меньше информации, чем верный.
EMISSION = {
    "correct": (0.85, 0.12),
    "partially_correct": (0.17, 0.35),
    "incorrect": (0.03, 0.50),
}
P_INIT = 0.25
P_TRANSIT = 0.12
MASTERY = 0.80


@dataclass
class ConceptState:
    p: float = P_INIT
    attempts: int = 0
    correct: int = 0
    turns: int = 0
    hint_level: int = 0
    deep: bool = False
    bottomed_out: bool = False
    completed: bool = False
    counterexample_used: set[str] = field(default_factory=set)
    active_misconceptions: set[str] = field(default_factory=set)
    resolved_misconceptions: set[str] = field(default_factory=set)
    asked: list[str] = field(default_factory=list)
    questions_used: set[str] = field(default_factory=set)
    covered: set[int] = field(default_factory=set)
    history: list[float] = field(default_factory=list)

    @property
    def mastered(self) -> bool:
        return self.p >= MASTERY and self.correct >= 1

    def coverage(self, n_points: int) -> float:
        return len(self.covered) / n_points if n_points else 0.0

    @property
    def done(self) -> bool:
        return self.mastered or self.bottomed_out or self.completed


@dataclass
class LearnerModel:
    concepts: dict[str, ConceptState]
    affect: list[str] = field(default_factory=list)
    off_topic_streak: int = 0
    ask_answer_count: int = 0
    confidence_pre: int | None = None
    confidence_post: int | None = None

    @classmethod
    def for_lesson(cls, concept_ids: list[str]) -> "LearnerModel":
        return cls({cid: ConceptState(history=[P_INIT]) for cid in concept_ids})

    def observe(self, cid: str, verdict: str, depth: int = 1, hint_level: int = 0) -> float:
        s = self.concepts[cid]
        if verdict not in EMISSION:
            return s.p
        pk, pu = EMISSION[verdict]
        if verdict == "correct":
            # Объяснение «почему» угадать сложнее, чем воспроизвести формулировку: глубокий ответ — более сильное свидетельство.
            if depth >= 2:
                pk, pu = 0.90, 0.06
                s.deep = True
            # Верный ответ после сильной подсказки говорит о знании меньше: подсказка «сделала часть работы».
            pu = min(0.6, pu / max(0.4, 1.0 - 0.18 * hint_level))
            s.correct += 1
        posterior = s.p * pk / (s.p * pk + (1 - s.p) * pu)
        transit = P_TRANSIT + (0.05 if s.hint_level else 0.0)
        s.p = round(posterior + (1 - posterior) * transit, 4)
        s.attempts += 1
        s.history.append(s.p)
        return s.p

    def mark_bottom_out(self, cid: str) -> None:
        s = self.concepts[cid]
        s.bottomed_out = True
        s.p = max(s.p, 0.55)
        s.history.append(s.p)

    def progress(self, cid: str) -> float:
        s = self.concepts[cid]
        if s.completed or s.bottomed_out:
            return max(1.0 if s.completed else 0.6, (s.p - P_INIT) / (MASTERY - P_INIT))
        return min(1.0, max(0.0, (s.p - P_INIT) / (MASTERY - P_INIT)))

    def overall(self, weights: dict[str, float]) -> float:
        tot = sum(weights.values()) or 1.0
        return sum(min(1.0, self.progress(c)) * w for c, w in weights.items()) / tot

    def recent_affect(self, n: int = 3) -> list[str]:
        return self.affect[-n:]
