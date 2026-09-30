from __future__ import annotations

import re
from dataclasses import dataclass, field

from .analyzer import Analysis
from .lesson import Lesson
from .llm import GigaChat, LLMResult, as_bool
from .policy import Plan

EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]+")
LIST_LINE = re.compile(r"^\s*([-*•#]|\d+[.)])\s+", re.M)
LONG_MOVES = {"bottom_out", "critique_example", "closing", "apply_case", "answer_question"}

SCHEMA = {
    "name": "audit_tutor_reply",
    "description": "Аудит черновика реплики сократического тьютора",
    "parameters": {
        "type": "object",
        "properties": {
            "reveals_answer": {"type": "boolean", "description": "Черновик прямо сообщает ученику ответ на текущий вопрос или называет ключевой пункт из списка «ещё не раскрыто»"},
            "false_praise": {"type": "boolean", "description": "Черновик называет неверный или неполный ответ ученика полностью верным или отличным. Признать верную часть ответа и прямо показать, чего не хватает, — НЕ ложная похвала"},
            "invented_facts": {"type": "boolean", "description": "Черновик сообщает факты, цифры, компании или даты, которых нет в тексте урока"},
            "ignores_move": {"type": "boolean", "description": "Черновик не выполняет заданный педагогический ход"},
            "comment": {"type": "string", "description": "Если есть нарушение — одно предложение, что исправить; иначе пусто"},
        },
        "required": ["reveals_answer", "false_praise", "invented_facts", "ignores_move", "comment"],
    },
}

SYSTEM = """Ты — строгий аудитор реплик сократического тьютора. Тьютор не должен выдавать ученику ответ, пока тот сам до него не дошёл (если ход не разрешает объяснить), не должен хвалить ошибку и выдумывать факты вне текста урока. Наводящий вопрос, пример или контрпример — это НЕ раскрытие ответа. Перефразирование того, что ученик сказал сам, — тоже не раскрытие. Отвечай через функцию audit_tutor_reply."""


@dataclass
class Verdict:
    ok: bool
    rule_issues: list[str] = field(default_factory=list)
    llm_issues: list[str] = field(default_factory=list)
    text: str = ""
    checked_by_llm: bool = False

    @property
    def feedback(self) -> str:
        return " ".join(self.rule_issues + self.llm_issues)


def clean(text: str) -> str:
    text = EMOJI.sub("", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^(Тьютор|Наставник|Майевтика|Росток)\s*:\s*", "", text.strip())
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def rule_check(text: str, plan: Plan) -> list[str]:
    issues = []
    q = text.count("?")
    words = len(text.split())
    if plan.move != "closing":
        if q == 0:
            issues.append("Реплика должна заканчиваться одним вопросом к ученику.")
        elif not re.search(r"\?[»\"')\s]*$", text):
            issues.append("Вопрос должен стоять в самом конце реплики.")
        if q > 2 or (q == 2 and plan.move not in ("critique_example",)):
            issues.append("В реплике больше одного вопроса — оставь ровно один.")
    limit = 150 if plan.move in LONG_MOVES else 85
    if words > limit:
        issues.append(f"Слишком длинно ({words} слов) — сократи до {limit}.")
    if plan.move not in ("critique_example", "closing") and LIST_LINE.search(text):
        issues.append("Без списков — пиши связным текстом.")
    return issues


def llm_check(llm: GigaChat, model: str, lesson: Lesson, plan: Plan, a: Analysis | None, draft: str, last_tutor: str, student: str, session_id: str | None = None, covered: set[int] | None = None) -> tuple[list[str], LLMResult]:
    c = lesson.concept(plan.target)
    covered = covered or set()
    missing = [p for i, p in enumerate(c.key_points, 1) if i not in covered]
    note = ""
    if plan.move == "affirm_advance":
        prev = next((ln.split(":", 1)[1].strip() for ln in plan.materials.splitlines() if ln.startswith("Только что пройденное понятие")), "")
        note = (f"\nОСОБЕННОСТЬ ХОДА: ученик только что освоил понятие «{prev}». Пересказ и похвала того, что ученик сам сказал о нём, — НЕ раскрытие ответа. "
                f"Раскрытием считается только подсказка ответа на НОВЫЙ вопрос — о понятии «{c.title}».")
    user = f"""ТЕКСТ УРОКА
{lesson.source_text}

ПЕДАГОГИЧЕСКИЙ ХОД: {plan.label}. Раскрывать суть {'можно' if plan.reveal_allowed else 'нельзя'}.{note}
Целевое понятие: {c.title}. Ученик ещё не раскрыл: {'; '.join(missing) or '—'}
Оценка ответа ученика диагностикой: {('верно (понятие раскрыто по совокупности ответов)' if plan.move == 'affirm_advance' else a.verdict) if a else '—'}

ПРЕДЫДУЩАЯ РЕПЛИКА ТЬЮТОРА
{last_tutor}

ОТВЕТ УЧЕНИКА
{student}

ЧЕРНОВИК НОВОЙ РЕПЛИКИ ТЬЮТОРА (проверь его)
{draft}"""
    res = llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], model=model, temperature=0.01, max_tokens=300, function=SCHEMA, session_id=session_id)
    r = res.args or {}
    issues = []
    if as_bool(r.get("reveals_answer")) and not plan.reveal_allowed:
        issues.append("Черновик раскрывает ответ — задай наводящий вопрос вместо ответа.")
    if as_bool(r.get("false_praise")) and a and a.verdict in ("incorrect", "partially_correct") and plan.move != "affirm_advance":
        issues.append("Черновик хвалит неверный или неполный ответ — будь честен и конкретен.")
    if as_bool(r.get("invented_facts")):
        issues.append("Черновик содержит факты вне текста урока — убери их.")
    if plan.move == "bottom_out" and as_bool(r.get("ignores_move")):
        issues.append("Ход требует прямо объяснить суть из материалов — объясни, а не задавай наводящий вопрос.")
    elif as_bool(r.get("ignores_move")):
        issues.append(f"Черновик не выполняет ход «{plan.label}».")
    if issues and r.get("comment"):
        issues.append(str(r["comment"])[:200])
    return issues, res


def needs_llm(plan: Plan, a: Analysis | None) -> bool:
    if plan.move in ("closing", "redirect", "summarize_prompt", "apply_case"):
        return False
    if plan.move == "bottom_out":
        return True
    return not plan.reveal_allowed or (a is not None and a.verdict in ("incorrect", "partially_correct"))
