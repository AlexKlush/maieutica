"""Автономный режим: тьютор без нейросети.

Нужен, чтобы приложение отвечало, даже когда GigaChat недоступен (нет ключа, закрыта сеть, сбой сервиса).
Диагностика реплики — по ключевым словам и пересечению с картой знаний урока; ответ собирается из банка вопросов,
подсказок и контрпримеров урока, то есть из тех же материалов, что политика отдаёт LLM. Политика, модель ученика
и проверка по правилам работают как обычно, поэтому ход диалога остаётся осмысленным, но реплики проще и однообразнее.
"""

from __future__ import annotations

import math
import re
import time

from .analyzer import Analysis
from .learner import LearnerModel
from .lesson import Lesson
from .llm import LLMResult, Usage
from .policy import Plan
from .verifier import LONG_MOVES

MODEL = "правила (офлайн)"

_WORD = re.compile(r"[а-яёa-z0-9]+", re.I)
_STOP = frozenset(
    "этот того этого этом этой этих этим тому чтобы который которые которая которое когда если только очень можно нужно такой такие такое "
    "также более свой свои свою тоже просто даже всех всего быть была были будет есть между через после перед чтоб чего тогда потом потому "
    "сейчас здесь меня тебя себя мне тебе хочу надо".split()
)
_END = re.compile(r"(ями|ами|ого|его|ому|ему|ыми|ими|ать|ять|ить|еть|ых|их|ов|ев|ей|ой|ий|ый|ая|яя|ое|ее|ие|ые|ую|юю|ом|ем|ам|ям|ах|ию|ья|ье|ью|ть|а|я|о|е|ы|и|у|ю|ь|й)$")

_MANIP = re.compile(
    r"забуд\w*\s+(вс\w+|предыдущ\w+|инструкц\w+|правил\w+)|игнорир\w+\s+(вс\w+|предыдущ\w+|инструкц\w+|правил\w+)|системн\w*\s+(промпт|сообщени\w+|инструкц\w+)"
    r"|\bпромпт|твои\s+инструкц|покажи\s+(свои\s+)?(инструкц|промпт|правил)|притворись|ты\s+теперь|режим\s+разработчик|ignore\s+(all|previous)|system\s+prompt|jailbreak"
)
_ASK = re.compile(
    r"скажи\w*\s+(мне\s+)?(правильн\w+\s+)?ответ|дай\w*\s+(мне\s+)?(правильн\w+\s+)?ответ|правильный\s+ответ|не\s+хочу\s+думать|некогда\s+думать|ответь\s+за\s+меня"
    r"|сам\w*\s+скажи|просто\s+скажи|скажи\s+как\s+правильно|расскажи\s+как\s+правильно|не\s+буду\s+думать"
)
_DONT = re.compile(r"\bне\s+знаю|не\s+помню|без\s+понятия|понятия\s+не\s+имею|затрудняюсь|не\s+уверен|не\s+понима\w+|не\s+могу\s+(ответить|сказать|вспомнить)|забыл\w*")
_STOPPING = re.compile(r"\b(хватит|стоп)\b|давайте?\s+(на\s+этом\s+)?(закончим|остановимся|завершим)|закончи(м|ть)\s+(занятие|урок|на\s+сегодня)|на\s+сегодня\s+(всё|все|хватит)|до\s+свидания")
_SOCIAL = re.compile(r"^\s*(привет\w*|здравствуй\w*|добрый\s+\w+|спасибо\w*|благодарю|пожалуйста|ок(ей)?|ладно|хорошо|понятно|ясно|ага|угу|да|нет)[\s!.,)]*$")
_ANGRY = re.compile(r"бесит|бесполезн\w+|трата\s+времени|надоел\w*|достал\w*|сколько\s+можно|задолбал\w*|отстань|скучн\w+|устал\w*|некогда|тороплюсь|спешу")
_ANXIOUS = re.compile(r"боюсь|не\s+уверен|вдруг\s+ошиб|переживаю|волнуюсь")
_CONFUSED = re.compile(r"запутал\w+|не\s+понима\w+|путаюсь|каша")
_CAUSAL = re.compile(r"потому\s+что|поэтому|так\s+как|чтобы|благодаря|иначе|следовательно|позволяет|за\s+счет|из-за")
_EXAMPLE = re.compile(r"например|допустим|представь\w*|скажем|к\s+примеру|в\s+моем|на\s+практике")
_QWORD = re.compile(r"^\s*(а\s+)?(что|как|почему|зачем|сколько|кто|когда|какой|какая|какие|какое|где|чем|можно\s+ли|нужно\s+ли|разве)\b")
_TASKS = re.compile(r"\b(провести|запустить|сделать|нанять|обновить|разработать|организовать|подготовить|внедрить|создать|написать)\b")


def _stem(w: str) -> str:
    cut = _END.sub("", w)
    return (cut if len(cut) >= 3 else w)[:6]


def stems(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text.lower().replace("ё", "е")):
        if w in _STOP or (len(w) < 4 and not w.isdigit()):
            continue
        out.add(_stem(w))
    return out


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е").strip()


def _vocab(lesson: Lesson) -> tuple[set[str], set[str]]:
    text = " ".join([lesson.title, lesson.source_text] + [f"{c.title} {c.expectation} {' '.join(c.key_points)}" for c in lesson.concepts])
    acronyms = {a.lower() for a in re.findall(r"\b[A-ZА-Я]{2,}\b", lesson.title + " " + lesson.source_text)}
    return stems(text), acronyms


def _covered(points: list[str], have: set[str]) -> list[int]:
    out = []
    for i, kp in enumerate(points, 1):
        ks = stems(kp)
        if ks and len(ks & have) >= max(1, math.ceil(0.4 * len(ks))):
            out.append(i)
    return out


def _misconceptions(lesson: Lesson, have: set[str], text: str) -> list[dict]:
    out = []
    for m in lesson.misconceptions:
        bs = stems(m.belief)
        if bs and len(bs & have) >= max(2, math.ceil(0.6 * len(bs))):
            out.append({"id": m.id, "quote": text.strip()[:120]})
    return out


def _result(args: dict | None, content: str, t0: float) -> LLMResult:
    return LLMResult(content=content, args=args, usage=Usage(), latency=time.perf_counter() - t0, model=MODEL)


def analyze(lesson: Lesson, target_id: str, phase: str, student_msg: str, active: list[str] | None = None, extra: str = "") -> tuple[Analysis, LLMResult]:
    """Диагностика реплики без нейросети. phase — ключ этапа (recall, explore, apply, …)."""
    t0 = time.perf_counter()
    t = _norm(student_msg)
    words = len(_WORD.findall(t))
    have = stems(t)
    vocab, acronyms = _vocab(lesson)
    on_topic = len(have & vocab)
    topical = bool(on_topic or any(a in t for a in acronyms))

    affect = "neutral"
    if _ANGRY.search(t):
        affect = "frustrated"
    elif _ANXIOUS.search(t):
        affect = "anxious"
    elif _CONFUSED.search(t):
        affect = "confused"
    elif words <= 2:
        affect = "bored"

    if _MANIP.search(t):
        intent = "manipulation"
    elif _STOPPING.search(t) and words <= 12:
        intent = "stop"
    elif _ASK.search(t):
        intent = "ask_answer"
    elif _DONT.search(t) and on_topic < 3 and words <= 20:
        intent = "dont_know"
    elif affect == "frustrated" and on_topic < 2:
        intent = "dont_know"
    elif _SOCIAL.match(t):
        intent = "social"
    elif not topical:
        intent = "off_topic" if words >= 2 else "social"
    elif _QWORD.match(t) or (t.endswith("?") and words <= 20):
        intent = "question"
    else:
        intent = "answer"

    c = lesson.concept(target_id)
    args: dict = {"intent": intent, "verdict": "not_applicable", "covered_points": [], "evidence": [], "misconceptions": [], "resolved": [], "depth": 1, "affect": affect,
                  "question_reveals_target": False, "diagnosis": ""}
    if intent == "question":
        args["question_reveals_target"] = len(stems(c.title + " " + " ".join(c.key_points)) & have) >= 2
        args["diagnosis"] = "Ученик задал вопрос по материалу."
    elif intent == "answer":
        covered = _covered(c.key_points, have)
        mis = _misconceptions(lesson, have, student_msg)
        verdict = "incorrect"
        if phase == "apply":
            covered, mis, verdict = _judge_application(lesson, t, words, extra)
        else:
            share = len(covered) / len(c.key_points) if c.key_points else 0.0
            if mis:
                verdict = "partially_correct" if share >= 0.5 else "incorrect"
            elif share >= 0.66:
                verdict = "correct"
            elif covered:
                verdict = "partially_correct"
            args["evidence"] = _evidence(lesson, target_id, have, mis)
        depth = 0 if words < 3 else 1
        if words >= 6 and _CAUSAL.search(t):
            depth = 2
            if words >= 15 and _EXAMPLE.search(t):
                depth = 3
        args.update(verdict=verdict, covered_points=covered, misconceptions=mis, depth=depth, diagnosis=f"Автономная диагностика: раскрыто пунктов {len(covered)} из {len(c.key_points)}.")
        if affect == "neutral" and depth >= 2:
            args["affect"] = "engaged"
    else:
        args["depth"] = 0
        args["diagnosis"] = "Автономная диагностика: реплика без содержательного ответа."
    return Analysis.from_args(args, lesson, len(c.key_points)), _result(args, "", t0)


def _evidence(lesson: Lesson, target_id: str, have: set[str], mis: list[dict]) -> list[dict]:
    out = []
    flagged = {cid for m in mis for cid in (lesson.misconception(m["id"]).concepts if lesson.misconception(m["id"]) else [])}
    app = lesson.curriculum[-1]
    for c in lesson.concepts:
        if c.id in (target_id, app):
            continue
        if c.id in flagged:
            out.append({"concept": c.id, "verdict": "incorrect"})
        elif c.key_points and len(_covered(c.key_points, have)) / len(c.key_points) >= 0.66:
            out.append({"concept": c.id, "verdict": "correct"})
    return out


def _judge_application(lesson: Lesson, t: str, words: int, extra: str) -> tuple[list[int], list[dict], str]:
    """Практика: решение кейса оцениваем по числам и глаголам-действиям, разбор чужого примера — по совпадению с известными ошибками."""
    if "УЧЕНИК ИЩЕТ ОШИБКИ" in extra:
        flaws = extra.split("Настоящие ошибки:", 1)[-1].split("\n", 1)[0].split("; ")
        hit = sum(1 for f in flaws if f.strip() and len(stems(f) & stems(t)) >= 1)
        verdict = "correct" if flaws and hit >= max(2, math.ceil(len(flaws) / 2)) else ("partially_correct" if hit or words >= 8 else "incorrect")
        return [], [], verdict
    if lesson.misconception("kr_are_tasks"):  # урок про цели и метрики: в решении должны быть числа, а не список дел
        numbers = len(re.findall(r"\d+", t))
        tasks = len(_TASKS.findall(t))
        mis = [{"id": "kr_are_tasks", "quote": t[:120]}] if tasks and numbers < 2 else []
        if words < 8:
            return [], mis, "incorrect"
        if numbers >= 3 and not tasks:
            return [1, 2, 3, 4], [], "correct"
        return [1], mis, "partially_correct"
    # любой другой материал: решение засчитываем, если оно по теме, конкретное и с объяснением «почему»
    on_topic = len(stems(t) & _vocab(lesson)[0])
    if words < 8:
        return [], [], "incorrect"
    if words >= 20 and on_topic >= 3 and (_CAUSAL.search(t) or _EXAMPLE.search(t)):
        return [1, 2, 3], [], "correct"
    return [1], [], "partially_correct"


# --- генерация реплики -------------------------------------------------------------------------------------------

_LEADS = {
    "probe_clarify": ("Есть верная мысль — давайте её уточним.", "Вы движетесь в нужную сторону, уточним один момент.", "Это уже часть картины."),
    "perspective": ("Посмотрим на это с другой стороны.", "Попробуем сменить точку зрения."),
    "probe_reasons": ("Верно. Но важно не только что, а почему.", "Так и есть — теперь пойдём глубже."),
    "probe_assumptions": ("В вашем рассуждении есть своя логика — давайте её проверим.", "Интересный ход мысли — а на чём он держится?"),
    "counterexample": ("Давайте проверим эту мысль на примере.", "Проверим это на конкретной ситуации."),
    "hint": ("Честно сказать «не знаю» — уже шаг вперёд. Давайте на шаг проще.", "Подскажу направление — дальше вы сами.", "Попробуем зайти с другой стороны, вместе."),
    "return_question": ("Хороший вопрос — попробуйте дойти до ответа сами.", "Вопрос хороший, и ответ вы можете найти сами."),
    "redirect": ("Давайте вернёмся к теме.", "Это немного в стороне от нашей темы."),
    "affirm_advance": ("Хорошо, с этим разобрались.", "Отлично, эта часть у вас есть."),
    "bottom_out": ("Вы хорошо поработали над этим — давайте я объясню, а вы потом перескажете.", "Не будем мучиться: это место непростое, объясню коротко."),
    "case_feedback": ("Спасибо, что попробовали.", "Есть с чем работать."),
    "metacognitive": ("Спасибо за итог.", "Хороший итог."),
    "summarize_prompt": ("Практика завершена.", "Кейсы позади."),
    "apply_case": ("Переходим к практике.",),
    "critique_example": ("Теперь обратная задача.",),
}
_EMPATHY = {
    "frustrated": "Вижу, что это уже утомляет, — и это честно. Сделаем шаг поменьше.",
    "anxious": "Сомневаться здесь нормально: мы как раз рассуждаем вслух, ошибки — часть пути.",
    "confused": "Здесь многие путаются, и это хороший знак — мысль работает. Сузим задачу до одного момента.",
    "bored": "Похоже, это для вас просто — тогда чуть острее.",
    "confident": "Вы уверенно держите материал — тогда чуть острее.",
}
_GENERIC_Q = {
    "probe_clarify": "Что ещё вы могли бы добавить к сказанному?",
    "perspective": "Как это выглядит с другой стороны — глазами того, кого это касается?",
    "probe_reasons": "Почему, по-вашему, это так?",
    "probe_assumptions": "На чём основан такой вывод — и что, если это допущение неверно?",
    "counterexample": "Всегда ли это так? Попробуйте придумать случай, где это не работает.",
    "hint": "Как бы вы теперь ответили?",
}


def _line(materials: str, marker: str) -> str:
    for line in materials.splitlines():
        if line.startswith(marker) and ":" in line:
            return line.split(":", 1)[1].strip()
    return ""


def _block(materials: str, start: str, end: str) -> str:
    return materials.split(start, 1)[1].split(end, 1)[0].strip() if start in materials else ""


def _pick(options: tuple[str, ...], n: int) -> str:
    return options[n % len(options)] if options else ""


def _from_text(lesson: Lesson, question: str) -> str:
    qs = stems(question)
    best, score = "", 0
    for sent in re.split(r"(?<=[.!?])\s+|\n+", lesson.source_text):
        sc = len(stems(sent) & qs)
        if sc > score:
            best, score = sent.strip(), sc
    return best


def _compose(lesson: Lesson, plan: Plan, a: Analysis | None, lm: LearnerModel, q: str, history: list[dict]) -> str:
    m = plan.materials or ""
    move = plan.move
    c = lesson.concept(plan.target)
    n = len(history)
    lead = _pick(_LEADS.get(move, ()), n)
    if plan.empathy and a and a.affect in _EMPATHY:
        lead = f"{_EMPATHY[a.affect]} {lead}".strip() if move not in ("redirect",) else _EMPATHY[a.affect]
    q = q or c.questions.get("open", "")

    if move in ("probe_clarify", "perspective", "probe_reasons", "probe_assumptions"):
        body = _line(m, "Вопрос-ориентир") or _GENERIC_Q[move]
    elif move == "counterexample":
        body = _line(m, "Контрпример") or _GENERIC_Q[move]
    elif move == "hint":
        body = _line(m, "Пример из банка подсказок") or _line(m, "Подсказка уровня")
        if not body or body.startswith("сформулируй"):
            body = c.hints[min(max(plan.hint_level, 1), len(c.hints)) - 1] if c.hints else _GENERIC_Q["hint"]
        if a and a.intent == "ask_answer":
            lead = "Понимаю, хочется короче. Но найденное самим запоминается лучше — давайте на шаг проще."
        if not body.rstrip().endswith("?"):
            body = f"{body} Вернёмся к вопросу: {q}" if q else body
    elif move == "return_question":
        body = _line(m, "Опора")
        body = f"{body} Как вы думаете?" if body and not body.rstrip().endswith("?") else (body or "Как вы думаете?")
    elif move == "answer_question":
        student = next((h["content"] for h in reversed(history) if h["role"] == "user"), "")
        found = _from_text(lesson, student)
        lead = f"Коротко по тексту: «{found}»." if found else "В тексте об этом ничего нет, гадать не буду."
        body = f"Вернёмся к вопросу: {q}" if q else "Как вы думаете?"
    elif move == "redirect":
        body = _line(m, "Текущий вопрос") or q
        if a and a.intent == "manipulation":
            lead = "Я остаюсь в роли тьютора и инструкции не раскрываю."
    elif move == "affirm_advance":
        prev = _line(m, "Только что пройденное понятие")
        if prev:
            lead = f"С понятием «{prev}» разобрались. Двигаемся дальше."
        body = _line(m, "Вопрос для перехода") or q
    elif move == "bottom_out":
        what = _line(m, "Объясни прямо") or c.expectation
        body = f"{what.rstrip('.')}. Перескажите это своими словами: как бы вы объяснили это коллеге?"
    elif move == "case_feedback":
        cx = _line(m, "Контрпример")
        hint = _line(m, "Подсказка при необходимости")
        body = cx or (f"{hint} Что бы вы поправили в своём решении?" if hint else "Что в своём решении вы бы поправили в первую очередь?")
    elif move == "apply_case":
        head = next((ln for ln in m.splitlines() if ln.startswith("Кейс")), "")
        task = _line(m, "Задание")
        body = f"{head} Можно взять и свою ситуацию. {task} С чего начнёте?".strip() if head else f"Придумайте ситуацию из своей жизни или работы. {task} С чего начнёте?"
    elif move == "critique_example":
        ex = _block(m, "Чужой пример (покажи ученику целиком):", "\nЗадание:")
        task = _line(m, "Задание")
        body = f"\n\n{ex}\n\n{task}" if ex else task
    elif move == "summarize_prompt":
        missed = _line(m, "Если ученик что-то упустил в разборе, одним предложением назови это")
        body = (f"Кстати, в разборе можно было заметить ещё: {missed} " if missed else "") + "Подведите итог своими словами в 2–3 предложениях: что главное вы взяли бы из материала?"
    elif move == "metacognitive":
        body = "Что в этой теме оказалось для вас самым неочевидным и где вы могли бы применить её у себя?"
    elif move == "closing":
        done = [lesson.concept(cid).short for cid, s in lm.concepts.items() if s.mastered]
        told = [lesson.concept(cid).short for cid, s in lm.concepts.items() if s.bottomed_out and not s.mastered]
        parts = ["Спасибо за занятие!"]
        if done:
            parts.append("Сами вы уверенно разобрали: " + ", ".join(done) + ".")
        parts.append(("Стоит ещё вернуться к: " + ", ".join(told) + ".") if told else "Это хороший результат.")
        return " ".join(parts)
    else:
        body = q
    return f"{lead} {body}".strip() if move != "critique_example" else f"{lead}{body}"


def _finish(text: str, plan: Plan, lesson: Lesson, q: str) -> str:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if plan.move == "closing":
        return text
    if not re.search(r"\?[»\"')\s]*$", text):
        text = f"{text} {q or lesson.concept(plan.target).questions.get('open', 'Как вы думаете?')}"
    if plan.move != "critique_example" and text.count("?") > 1:
        head, _, tail = text.rpartition("?")
        text = head.replace("?", ".") + "?" + tail
    limit = 150 if plan.move in LONG_MOVES else 85
    while len(text.split()) > limit and ". " in text.split("?")[0]:
        text = text.split(". ", 1)[1]
    return text


def generate(lesson: Lesson, plan: Plan, a: Analysis | None, lm: LearnerModel, last_question: str, history: list[dict]) -> LLMResult:
    """Реплика тьютора без нейросети: ход выбрала политика, формулировки берём из банка урока."""
    t0 = time.perf_counter()
    text = _finish(_compose(lesson, plan, a, lm, last_question, history), plan, lesson, last_question)
    return _result(None, text, t0)
