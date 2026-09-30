from __future__ import annotations

import re

from . import materials
from .lesson import Lesson
from .llm import GigaChat, LLMError

SYSTEM = """Ты — методист, который готовит сценарий сократического диалога по учебному тексту (в духе expectation–misconception tailored dialogue из AutoTutor). Строишь карту знаний строго по тексту: никаких внешних фактов, цифр и примеров компаний, которых нет в тексте (кроме нейтральных учебных ситуаций в кейсах).

Требования:
- title — короткое название темы, 2–5 слов.
- 3–6 понятий в порядке изучения (меньше — для короткого текста); последнее понятие всегда «Применение» (id: application, bloom: create) — ученик применяет материал к новой ситуации.
- Для каждого понятия: id латиницей (snake_case), короткое название (1–2 слова), ожидаемое понимание (1–2 предложения), 2–4 ключевых пункта, вопросы open / reasons / implications (открытые, без подсказки ответа, каждый — одно предложение с одним вопросительным знаком), 3 подсказки по возрастанию силы (от наводящего вопроса к почти готовому ответу).
- 2–5 типичных заблуждений: во что ученик может ошибочно поверить после прочтения, почему это неверно, и контрпример-вопрос, который столкнёт ученика с противоречием (приём эленхоса).
- 1–2 кейса для применения и 1 пример с ошибками, который ученик должен разобрать.
- Всё на русском языке. Вопросы обращены к ученику на «вы». Пиши кратко."""

FUNCTION = {
    "name": "build_lesson_map",
    "description": "Карта знаний урока для сократического диалога",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "goal": {"type": "string", "description": "Что ученик должен уметь после занятия, 1 предложение"},
            "promise": {"type": "string", "description": "Обещание ученику в начале, начинается с «К концу разговора вы сможете»"},
            "concepts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "short": {"type": "string"},
                        "bloom": {"type": "string", "enum": ["remember", "understand", "apply", "analyze", "evaluate", "create"]},
                        "expectation": {"type": "string"},
                        "key_points": {"type": "array", "items": {"type": "string"}},
                        "q_open": {"type": "string"},
                        "q_reasons": {"type": "string"},
                        "q_implications": {"type": "string"},
                        "hints": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "title", "short", "bloom", "expectation", "key_points", "q_open", "q_reasons", "q_implications", "hints"],
                },
            },
            "misconceptions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "concepts": {"type": "array", "items": {"type": "string"}},
                        "belief": {"type": "string"},
                        "why_wrong": {"type": "string"},
                        "counterexample": {"type": "string"},
                    },
                    "required": ["id", "concepts", "belief", "why_wrong", "counterexample"],
                },
            },
            "cases": {
                "type": "array",
                "items": {"type": "object", "properties": {"title": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["title", "prompt"]},
            },
            "flawed_examples": {
                "type": "array",
                "items": {"type": "object", "properties": {"text": {"type": "string"}, "flaws": {"type": "array", "items": {"type": "string"}}}, "required": ["text", "flaws"]},
            },
        },
        "required": ["title", "goal", "promise", "concepts", "misconceptions", "cases", "flawed_examples"],
    },
}


BLOOM = ("remember", "understand", "apply", "analyze", "evaluate", "create")


def _slug(s: str, i: int) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")
    return s or f"c{i}"


def compile_lesson(llm: GigaChat, text: str, model: str = "GigaChat-2-Max", timeout: float | None = None) -> tuple[Lesson, str]:
    """Карта урока от GigaChat и модель, которая её собрала. Пропуски в ответе модели закрываем шаблонами из materials."""
    res = llm.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"ТЕКСТ УРОКА\n{text.strip()}"}],
        model=model,
        temperature=0.2,
        max_tokens=3500,
        function=FUNCTION,
        timeout=timeout,
    )
    d = res.args or {}
    raw = [c for c in d.get("concepts", []) if isinstance(c, dict) and (c.get("title") or c.get("expectation"))]
    if len(raw) > 7:  # модель увлеклась: оставляем первые шесть и «Применение»
        raw = raw[:6] + [raw[-1]]
    concepts, seen = [], set()
    for i, c in enumerate(raw):
        cid = _slug(str(c.get("id", "")), i)
        while cid in seen:
            cid += "_"
        seen.add(cid)
        title = str(c.get("title") or cid)
        expectation = str(c.get("expectation") or "")
        questions = {k: str(v) for k, v in {"open": c.get("q_open"), "reasons": c.get("q_reasons"), "implications": c.get("q_implications")}.items() if v}
        questions.setdefault("open", materials.open_question(i, title))
        concepts.append({
            "id": cid,
            "title": title,
            "short": str(c.get("short") or title)[:22],
            "bloom": c.get("bloom") if c.get("bloom") in BLOOM else "understand",
            "expectation": expectation,
            "key_points": [str(p) for p in c.get("key_points", []) if p][:5] or [expectation or title],
            "questions": questions,
            "hints": [str(h) for h in c.get("hints", []) if h][:3] or [f"Найдите в тексте место про «{title}»: как бы вы пересказали его своими словами?"],
            "core": True,
        })
    if len(concepts) < 2:
        raise LLMError("не удалось построить карту знаний по тексту")
    if concepts[-1]["bloom"] not in ("create", "apply"):
        concepts.append(materials.application())
    ids = {c["id"] for c in concepts}
    misconceptions = []
    for i, m in enumerate(d.get("misconceptions", [])):
        if not isinstance(m, dict) or not m.get("belief"):
            continue
        linked = [_slug(str(x), 0) for x in m.get("concepts", []) if _slug(str(x), 0) in ids] or [concepts[0]["id"]]
        misconceptions.append({"id": _slug(str(m.get("id", "")), i) or f"m{i}", "concepts": linked, "belief": str(m["belief"]),
                               "why_wrong": str(m.get("why_wrong", "")), "counterexample": str(m.get("counterexample", ""))})
    cases = [{"id": f"case{i}", "title": str(c.get("title", "")), "prompt": str(c["prompt"])}
             for i, c in enumerate(d.get("cases", [])) if isinstance(c, dict) and c.get("prompt")]
    flawed = [{"id": f"flaw{i}", "okr": str(f["text"]), "flaws": [str(x) for x in f.get("flaws", [])]}
              for i, f in enumerate(d.get("flawed_examples", [])) if isinstance(f, dict) and f.get("text")]
    lesson = Lesson.model_validate({
        "id": materials.lesson_id(text),
        "title": str(d.get("title") or materials.title_of(text))[:80],
        "source_text": text.strip(),
        "goal": str(d.get("goal") or materials.GOAL),
        "promise": str(d.get("promise") or materials.PROMISE),
        "application_task": materials.APPLICATION_TASK,
        "concepts": concepts,
        "misconceptions": misconceptions,
        "cases": cases,
        "flawed_examples": flawed,
        "curriculum": [c["id"] for c in concepts],
    })
    return lesson, res.model.split(":")[0]
