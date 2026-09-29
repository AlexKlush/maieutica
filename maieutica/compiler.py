from __future__ import annotations

import re

from .lesson import Lesson
from .llm import GigaChat, LLMError

SYSTEM = """Ты — методист, который готовит сценарий сократического диалога по учебному тексту (в духе expectation–misconception tailored dialogue из AutoTutor). Строишь карту знаний строго по тексту: никаких внешних фактов, цифр и примеров компаний, которых нет в тексте (кроме нейтральных учебных ситуаций в кейсах).

Требования:
- 5–8 понятий в порядке изучения; последнее понятие всегда «Применение» (id: application, bloom: create) — ученик применяет материал к новой ситуации.
- Для каждого понятия: id латиницей (snake_case), короткое название (1–2 слова), ожидаемое понимание (1–2 предложения), 3–4 ключевых пункта, вопросы open / reasons / implications (открытые, без подсказки ответа), 3 подсказки по возрастанию силы (от наводящего вопроса к почти готовому ответу).
- 4–8 типичных заблуждений: во что ученик может ошибочно поверить после прочтения, почему это неверно, и контрпример-вопрос, который столкнёт ученика с противоречием (приём эленхоса).
- 2 кейса для применения и 1–2 примера с ошибками, которые ученик должен найти.
- Всё на русском языке. Вопросы обращены к ученику на «вы»."""

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


def _slug(s: str, i: int) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")
    return s or f"c{i}"


def compile_lesson(llm: GigaChat, text: str, model: str = "GigaChat-2-Max") -> Lesson:
    res = llm.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"ТЕКСТ УРОКА\n{text.strip()}"}],
        model=model,
        temperature=0.2,
        max_tokens=4000,
        function=FUNCTION,
    )
    d = res.args or {}
    concepts, seen = [], set()
    for i, c in enumerate(d.get("concepts", [])):
        cid = _slug(c.get("id", ""), i)
        while cid in seen:
            cid += "_"
        seen.add(cid)
        concepts.append({
            "id": cid,
            "title": c.get("title", cid),
            "short": c.get("short") or c.get("title", cid)[:18],
            "bloom": c.get("bloom", "understand"),
            "expectation": c.get("expectation", ""),
            "key_points": [p for p in c.get("key_points", []) if p][:5] or [c.get("expectation", "")],
            "questions": {k: v for k, v in {"open": c.get("q_open"), "reasons": c.get("q_reasons"), "implications": c.get("q_implications")}.items() if v},
            "hints": [h for h in c.get("hints", []) if h][:3],
            "core": True,
        })
    if len(concepts) < 2:
        raise LLMError("Не удалось построить карту знаний по тексту")
    if concepts[-1]["bloom"] not in ("create", "apply"):
        concepts[-1]["bloom"] = "create"
    ids = {c["id"] for c in concepts}
    misconceptions = []
    for i, m in enumerate(d.get("misconceptions", [])):
        linked = [_slug(x, 0) for x in m.get("concepts", []) if _slug(x, 0) in ids] or [concepts[0]["id"]]
        misconceptions.append({"id": _slug(m.get("id", ""), i) or f"m{i}", "concepts": linked, "belief": m.get("belief", ""), "why_wrong": m.get("why_wrong", ""), "counterexample": m.get("counterexample", "")})
    cases = [{"id": f"case{i}", "title": c.get("title", ""), "prompt": c.get("prompt", "")} for i, c in enumerate(d.get("cases", [])) if c.get("prompt")]
    flawed = [{"id": f"flaw{i}", "okr": f.get("text", ""), "flaws": f.get("flaws", [])} for i, f in enumerate(d.get("flawed_examples", [])) if f.get("text")]
    return Lesson.model_validate({
        "id": "custom",
        "title": d.get("title", "Ваш материал"),
        "source_text": text.strip(),
        "goal": d.get("goal", ""),
        "promise": d.get("promise", ""),
        "concepts": concepts,
        "misconceptions": misconceptions,
        "cases": cases,
        "flawed_examples": flawed,
        "curriculum": [c["id"] for c in concepts],
    })
