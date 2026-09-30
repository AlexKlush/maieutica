from __future__ import annotations

import copy
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import analyzer, generator, offline, verifier
from .analyzer import Analysis
from .learner import LearnerModel
from .lesson import Lesson
from .llm import GigaChat, LLMError, LLMResult, LLMUnavailable
from .policy import COMPLETE, PHASES, DialogueState, Plan, Policy

DEFAULT_MODELS = {"analyzer": "GigaChat-2-Max", "generator": "GigaChat-2-Max", "verifier": "GigaChat-2-Max"}


@dataclass
class Stage:
    key: str
    label: str
    model: str = ""
    latency: float = 0.0
    queued: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    note: str = ""

    @classmethod
    def from_result(cls, key: str, label: str, r: LLMResult, note: str = "") -> "Stage":
        if r.requested:
            note = "; ".join(x for x in (note, f"замена: {r.requested} недоступна") if x)
        return cls(key, label, r.model, round(r.latency, 2), round(r.queued, 2), r.usage.prompt, r.usage.completion, r.usage.cached, note)


@dataclass
class TurnTrace:
    turn: int
    student: str
    reply: str
    phase: str
    target: str
    move: str
    move_label: str
    rationale: str
    tags: list[str]
    analysis: dict | None
    stages: list[Stage] = field(default_factory=list)
    verifier: dict = field(default_factory=dict)
    mastery: dict[str, float] = field(default_factory=dict)
    total_latency: float = 0.0
    offline: bool = False
    note: str = ""

    @property
    def tokens(self) -> int:
        return sum(s.prompt_tokens + s.completion_tokens for s in self.stages)


def last_question(text: str) -> str:
    import re

    parts = re.split(r"(?<=[.!?])\s+", text.strip())  # не на «…»: «и… какое ограничение?» — один вопрос
    qs = [p for p in parts if p.endswith("?")]
    return qs[-1] if qs else ""


class Tutor:
    def __init__(self, lesson: Lesson, llm: GigaChat | None, models: dict | None = None, address: str = "вы", verify: bool = True, allow_offline: bool = True):
        self.lesson = lesson
        self.llm = llm
        # allow_offline=False — для оценки качества (eval): любой сбой LLM должен быть виден, а не подменяться правилами.
        self.allow_offline = allow_offline
        self.offline_reason = "" if llm else "ключ GIGACHAT_CREDENTIALS не задан"
        self.models = {**DEFAULT_MODELS, **(models or {})}
        self.address = address
        self.verify = verify
        self.session_id = str(uuid.uuid4())
        self.lm = LearnerModel.for_lesson([c.id for c in lesson.concepts])
        self.state = DialogueState(target=lesson.curriculum[0])
        self.policy = Policy(lesson)
        self.history: list[dict] = []
        self.traces: list[TurnTrace] = []
        self.finished = False

    def start(self) -> TurnTrace:
        plan = self.policy.open_plan(self.lm)
        text = generator.opening(self.lesson, self.address)
        self.history.append({"role": "assistant", "content": text})
        self.state.last_question = last_question(text)
        tr = self._trace("", text, plan, None, [], {"ok": True, "skipped": "шаблонная реплика"}, 0.0)
        self.traces.append(tr)
        return tr

    def step(self, student: str, on_stage: Callable[[str, str], None] | None = None) -> TurnTrace:
        snapshot = (copy.deepcopy(self.lm), copy.deepcopy(self.state), list(self.history))
        try:
            return self._step(student, on_stage)
        except Exception:
            self.lm, self.state, self.history = snapshot
            raise

    def _step(self, student: str, on_stage: Callable[[str, str], None] | None = None) -> TurnTrace:
        notify = on_stage or (lambda k, l: None)
        t0 = time.perf_counter()
        stages: list[Stage] = []
        last_tutor = next((m["content"] for m in reversed(self.history) if m["role"] == "assistant"), "")
        prior = list(self.history)
        self.history.append({"role": "user", "content": student.strip()})

        notify("analyze", "Диагностика ответа")
        active = sorted({m for s in self.lm.concepts.values() for m in s.active_misconceptions})
        a, res = self._analyze(student, last_tutor, prior, active)
        stages.append(Stage.from_result("analyze", "Диагностика", res))

        notify("model", "Обновление модели ученика")
        self._update_learner(a)

        notify("plan", "Выбор педагогического хода")
        tp = time.perf_counter()
        plan = self.policy.decide(self.state, self.lm, a)
        stages.append(Stage("plan", "Политика", "правила", round(time.perf_counter() - tp, 4), note=plan.label))

        text, vinfo = self._compose(plan, a, last_tutor, student, stages, notify)
        self.history.append({"role": "assistant", "content": text})
        self.state.last_question = last_question(text)
        if plan.end:
            self.finished = True
        tr = self._trace(student, text, plan, a, stages, vinfo, time.perf_counter() - t0)
        self.traces.append(tr)
        return tr

    def jump(self, phase: str, on_stage: Callable[[str, str], None] | None = None) -> TurnTrace:
        notify = on_stage or (lambda k, l: None)
        t0 = time.perf_counter()
        for cid in self.policy.explorable():
            s = self.lm.concepts[cid]
            if not s.done:
                s.bottomed_out = True
        if phase == "apply":
            plan = self.policy._start_apply(self.state, self.lm, "Демо-режим: переход к практике по кнопке.")
        else:
            plan = self.policy._start_reflect(self.state, "Демо-режим: переход к рефлексии по кнопке.")
        stages: list[Stage] = []
        self.history.append({"role": "user", "content": "(давайте перейдём к следующему этапу)"})
        text, vinfo = self._compose(plan, None, "", "", stages, notify)
        self.history.append({"role": "assistant", "content": text})
        self.state.last_question = last_question(text)
        tr = self._trace("", text, plan, None, stages, vinfo, time.perf_counter() - t0)
        self.traces.append(tr)
        return tr

    def _update_learner(self, a: Analysis) -> None:
        tid = self.state.target
        s = self.lm.concepts[tid]
        if a.intent in ("answer", "dont_know", "ask_answer"):
            s.turns += 1
        if a.intent == "answer" and a.verdict != "not_applicable":
            s.covered |= set(a.covered_points)
            verdict = a.verdict
            if verdict == "partially_correct" and self.policy.coverage(self.lm, tid) >= COMPLETE:
                verdict = "correct"
            self.lm.observe(tid, verdict, a.depth, s.hint_level)
            if a.verdict == "correct" and s.active_misconceptions:
                s.resolved_misconceptions |= s.active_misconceptions
                s.active_misconceptions = set()
        app = self.policy.app_id
        for e in a.evidence:
            cid = e["concept"]
            if cid == tid or (cid == app and self.state.phase != "apply"):
                continue
            if self.state.phase == "recall" or e["verdict"] in ("correct", "incorrect"):
                if e["verdict"] == "correct":
                    self.lm.concepts[cid].covered |= set(range(1, len(self.lesson.concept(cid).key_points) + 1))
                self.lm.observe(cid, e["verdict"], a.depth, self.lm.concepts[cid].hint_level)
        for mid in a.resolved:
            for s2 in self.lm.concepts.values():
                if mid in s2.active_misconceptions:
                    s2.active_misconceptions.discard(mid)
                    s2.resolved_misconceptions.add(mid)
        for m in a.misconceptions:
            mis = self.lesson.misconception(m["id"])
            if mis:
                for cid in mis.concepts:
                    if cid in self.lm.concepts:
                        self.lm.concepts[cid].active_misconceptions.add(mis.id)
        self.lm.affect.append(a.affect)

    def _compose(self, plan: Plan, a: Analysis | None, last_tutor: str, student: str, stages: list[Stage], notify) -> tuple[str, dict]:
        notify("generate", "Генерация реплики")
        g = self._generate(plan, a)
        stages.append(Stage.from_result("generate", "Генерация", g))
        text = verifier.clean(g.content)
        issues = verifier.rule_check(text, plan)
        llm_issues: list[str] = []
        checked = False
        if not issues and self.verify and self._online() and verifier.needs_llm(plan, a):
            notify("verify", "Проверка на утечку ответа")
            try:
                llm_issues, vr = verifier.llm_check(self.llm, self.models["verifier"], self.lesson, plan, a, text, last_tutor, student, self.session_id, covered=self.lm.concepts[plan.target].covered)
                stages.append(Stage.from_result("verify", "Проверка", vr, "ok" if not llm_issues else "отклонено"))
                checked = True
            except LLMError as e:
                if not self.allow_offline:
                    raise
                self._degrade(e)
        info = {"ok": not (issues or llm_issues), "rule_issues": issues, "llm_issues": llm_issues, "checked_by_llm": checked, "regenerated": False, "fallback": False, "draft": ""}
        if (issues or llm_issues) and g.model != offline.MODEL:
            notify("regenerate", "Исправление черновика")
            info["draft"] = text
            feedback = " ".join(issues + llm_issues)
            g2 = self._generate(plan, a, feedback, temperature=0.4)
            stages.append(Stage.from_result("regenerate", "Перегенерация", g2, feedback[:120]))
            text2 = verifier.clean(g2.content)
            issues2 = verifier.rule_check(text2, plan)
            info["regenerated"] = True
            severe = [i for i in issues2 if "длинно" not in i]
            if severe:
                fb = self._fallback(plan)
                if fb:
                    text2 = fb
                    info["fallback"] = True
            info["rule_issues_after"] = issues2
            text = text2
        return text, info

    def _fallback(self, plan: Plan) -> str:
        lines = plan.materials.splitlines()
        for marker in ("Пример из банка подсказок", "Подсказка уровня", "Вопрос для перехода:", "Вопрос-ориентир", "Контрпример:", "Текущий вопрос:"):
            for line in lines:
                if line.startswith(marker) or (marker.endswith(":") and marker in line):
                    q = line.split(":", 1)[1].strip() if not marker.endswith(":") else line.split(marker, 1)[1].strip()
                    if marker == "Подсказка уровня" and q.startswith("сформулируй"):
                        continue
                    if marker == "Вопрос для перехода:":
                        return "Хорошо, двигаемся дальше. " + q
                    if q and not q.rstrip().endswith("?") and self.state.last_question:
                        q = f"{q} {self.state.last_question}"
                    return q
        return ""

    def _online(self) -> bool:
        """Идём ли в GigaChat: ключ есть и связь не оборвалась недавно. При allow_offline=False сбой не скрываем — всегда идём в LLM."""
        if self.llm is None:
            if not self.allow_offline:
                raise LLMUnavailable(self.offline_reason)
            return False
        return not self.allow_offline or self.llm.available()

    @property
    def offline(self) -> bool:
        return not self._online()

    def _degrade(self, e: LLMError) -> None:
        self.offline_reason = str(e)

    def _analyze(self, student: str, last_tutor: str, prior: list[dict], active: list[str]) -> tuple[Analysis, LLMResult]:
        if self._online():
            try:
                return analyzer.analyze(self.llm, self.models["analyzer"], self.lesson, self.state.target, PHASES[self.state.phase], last_tutor, student, prior, self.session_id, extra=self._extra(), active=active)
            except LLMError as e:
                if not self.allow_offline:
                    raise
                self._degrade(e)
        return offline.analyze(self.lesson, self.state.target, self.state.phase, student, active, self._extra())

    def _generate(self, plan: Plan, a: Analysis | None, feedback: str = "", temperature: float = 0.55) -> LLMResult:
        if self._online():
            try:
                return generator.generate(self.llm, self.models["generator"], self.lesson, plan, a, self.lm, self.history, self.address, feedback=feedback, session_id=self.session_id, temperature=temperature)
            except LLMError as e:
                if not self.allow_offline:
                    raise
                self._degrade(e)
        return offline.generate(self.lesson, plan, a, self.lm, self.state.last_question, self.history)

    def _extra(self) -> str:
        st = self.state
        if st.phase == "apply" and st.apply_step == 0:
            case = next((c for c in self.lesson.cases if c.id == st.case_id), None)
            if case:
                return ("КРИТЕРИИ ПРОВЕРКИ РЕШЕНИЯ КЕЙСА\nХорошо: " + "; ".join(case.good_signals) + "\nОшибки: " + "; ".join(case.bad_signals)
                        + "\nПроверь КАЖДЫЙ ключевой результат: это измеримый результат или действие/задача («провести», «запустить», «сделать»)? Действие в роли KR — это заблуждение kr_are_tasks.")
        if st.phase == "apply" and st.apply_step == 1:
            ex = next((e for e in self.lesson.flawed_examples if e.id == st.flawed_id), None)
            if ex:
                return "УЧЕНИК ИЩЕТ ОШИБКИ В ЧУЖОМ ПРИМЕРЕ\n" + ex.okr + "\nНастоящие ошибки: " + "; ".join(ex.flaws) + "\ncorrect — ученик нашёл большинство ошибок; partially_correct — одну."
        return ""

    def _trace(self, student: str, reply: str, plan: Plan, a: Analysis | None, stages: list[Stage], vinfo: dict, latency: float) -> TurnTrace:
        by_rules = any(st.model == offline.MODEL for st in stages)
        return TurnTrace(
            turn=len(self.traces),
            student=student,
            reply=reply,
            phase=plan.phase,
            target=plan.target,
            move=plan.move,
            move_label=plan.label,
            rationale=plan.rationale,
            tags=list(plan.tags),
            analysis=asdict(a) if a else None,
            stages=stages,
            verifier=vinfo,
            mastery={cid: round(s.p, 3) for cid, s in self.lm.concepts.items()},
            total_latency=round(latency, 2),
            offline=by_rules,
            note=self.offline_reason if by_rules else "",
        )

    def export(self) -> dict:
        return {
            "lesson": self.lesson.id,
            "session": self.session_id,
            "models": self.models,
            "turns": [asdict(t) for t in self.traces],
            "final_mastery": {cid: round(s.p, 3) for cid, s in self.lm.concepts.items()},
            "misconceptions": {cid: {"active": sorted(s.active_misconceptions), "resolved": sorted(s.resolved_misconceptions)} for cid, s in self.lm.concepts.items()},
        }
