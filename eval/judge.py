from __future__ import annotations

import argparse
import json
from pathlib import Path

from maieutica import GigaChat, load_lesson

from .personas import PERSONAS

EVAL = Path(__file__).resolve().parent
RUNS = EVAL / "runs"
JUDGMENTS = EVAL / "judgments"

RUBRIC = """Рубрика основана на таксономии педагогических способностей AI-тьюторов (Maurya et al., «Unifying AI Tutor Evaluation», NAACL 2025) и дополнена критериями сократического диалога.

ОЦЕНКА КАЖДОЙ РЕПЛИКИ ТЬЮТОРА (кроме первой, приветственной). Значения: yes / partly / no / na.
- mistake_identification: если в предыдущей реплике ученика была ошибка или неточность — заметил ли её тьютор? na, если ошибки не было.
- mistake_location: указал ли тьютор (прямо или вопросом), ГДЕ именно ошибка? na, если ошибки не было.
- revealing_answer: выдал ли тьютор готовый ответ на свой вопрос или на то, до чего ученик должен был дойти сам? yes — плохо. Если ученик уже дошёл сам или тьютор объясняет после долгого затруднения и просит пересказать — no.
- providing_guidance: даёт ли реплика полезную опору (наводящий вопрос, пример, контрпример, подсказку), а не просто «подумайте ещё»?
- actionability: понятно ли ученику, что делать дальше?
- coherence: логично ли реплика продолжает диалог и отвечает на то, что сказал ученик?
- tutor_tone: encouraging / neutral / offensive.
- humanlike: звучит ли как живой внимательный преподаватель, а не шаблонный бот?
- empathy: если у ученика были эмоции (неуверенность, раздражение, скука) — отреагировал ли тьютор уместно? na, если эмоций не было.

ОЦЕНКА ДИАЛОГА ЦЕЛИКОМ (1–5):
- learning_progress: насколько диалог продвинул понимание материала учеником (судя по его репликам).
- socratic_quality: вопросы ведут ученика к самостоятельному выводу; есть уточнение, запрос обоснований, контрпримеры; нет лекций и викторины.
- empathy: поддержка без сюсюканья, реакция на эмоции.
- goal_orientation: диалог держит цель — освоить материал, последовательно покрывает ключевые идеи, не буксует и не уходит в сторону.
- robustness: устойчивость к провокациям, офтопу, просьбам выдать ответ (5, если провокаций не было и всё в порядке).
- concepts_covered: какие понятия урока были содержательно проработаны с учеником (id из списка)."""

TURN_ITEM = {
    "type": "object",
    "properties": {
        "turn": {"type": "integer"},
        **{k: {"type": "string", "enum": ["yes", "partly", "no", "na"]} for k in ["mistake_identification", "mistake_location", "revealing_answer", "providing_guidance", "actionability", "coherence", "humanlike", "empathy"]},
        "tutor_tone": {"type": "string", "enum": ["encouraging", "neutral", "offensive"]},
    },
    "required": ["turn", "mistake_identification", "mistake_location", "revealing_answer", "providing_guidance", "actionability", "coherence", "tutor_tone", "humanlike", "empathy"],
}


def dialogue_schema(concept_ids: list[str]) -> dict:
    return {
        "name": "judge_dialogue",
        "description": "Оценка сократического диалога по рубрике",
        "parameters": {
            "type": "object",
            "properties": {
                "turns": {"type": "array", "items": TURN_ITEM},
                **{k: {"type": "integer", "minimum": 1, "maximum": 5} for k in ["learning_progress", "socratic_quality", "empathy", "goal_orientation", "robustness"]},
                "concepts_covered": {"type": "array", "items": {"type": "string", "enum": concept_ids}},
                "comment": {"type": "string"},
            },
            "required": ["turns", "learning_progress", "socratic_quality", "empathy", "goal_orientation", "robustness", "concepts_covered", "comment"],
        },
    }


PAIR_SCHEMA = {
    "name": "compare_dialogues",
    "description": "Попарное сравнение двух диалогов с одним и тем же учеником",
    "parameters": {
        "type": "object",
        "properties": {
            **{k: {"type": "string", "enum": ["A", "B", "tie"]} for k in ["learning_progress", "socratic_quality", "empathy", "goal_orientation", "robustness", "overall"]},
            "rationale": {"type": "string"},
        },
        "required": ["learning_progress", "socratic_quality", "empathy", "goal_orientation", "robustness", "overall", "rationale"],
    },
}


def render(dialogue: list[dict]) -> str:
    out, n = [], 0
    for m in dialogue:
        if m["role"] == "tutor":
            out.append(f"[Тьютор, реплика {n}] {m['content']}")
            n += 1
        else:
            out.append(f"[Ученик] {m['content']}")
    return "\n".join(out)


def lesson_block(lesson) -> str:
    return "ТЕКСТ УРОКА\n" + lesson.source_text + "\n\nПОНЯТИЯ УРОКА (id — название)\n" + "\n".join(f"{c.id} — {c.title}" for c in lesson.concepts)


def judge_dialogue(llm: GigaChat, model: str, lesson, run: dict) -> dict:
    persona = next(p for p in PERSONAS if p["id"] == run["persona"])
    user = f"""{lesson_block(lesson)}

УЧЕНИК В ДИАЛОГЕ: {persona['role']} — {persona['note']}

ДИАЛОГ
{render(run['dialogue'])}

Оцени каждую реплику тьютора, начиная с реплики 1 (реплику 0 не оценивай), и диалог целиком."""
    res = llm.chat(
        [{"role": "system", "content": "Ты — эксперт по педагогике и оценке AI-тьюторов. Оценивай строго и беспристрастно, не завышай. Не знаешь, какая система породила диалог.\n\n" + RUBRIC},
         {"role": "user", "content": user}],
        model=model, temperature=0.01, max_tokens=3500, function=dialogue_schema([c.id for c in lesson.concepts]),
    )
    return res.args or {}


def compare(llm: GigaChat, model: str, lesson, a: dict, b: dict) -> dict:
    persona = next(p for p in PERSONAS if p["id"] == a["persona"])
    user = f"""{lesson_block(lesson)}

УЧЕНИК: {persona['role']} — {persona['note']}

ДИАЛОГ A
{render(a['dialogue'])}

ДИАЛОГ B
{render(b['dialogue'])}

Какой тьютор лучше по каждому критерию? Порядок A/B случаен и ничего не значит; длина диалога сама по себе не достоинство."""
    res = llm.chat(
        [{"role": "system", "content": "Ты — эксперт по педагогике и оценке AI-тьюторов. Сравниваешь два диалога с одним и тем же симулированным учеником.\n\n" + RUBRIC},
         {"role": "user", "content": user}],
        model=model, temperature=0.01, max_tokens=800, function=PAIR_SCHEMA,
    )
    return res.args or {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="main")
    ap.add_argument("--model", default="GigaChat-2-Max")
    ap.add_argument("--judge-name", default="gigachat")
    args = ap.parse_args()
    llm = GigaChat()
    lesson = load_lesson("okr")
    runs_dir = RUNS / args.tag
    out = JUDGMENTS / args.tag / args.judge_name
    (out / "pairwise").mkdir(parents=True, exist_ok=True)
    runs = {p.stem: json.loads(p.read_text()) for p in sorted(runs_dir.glob("*.json"))}
    for name, run in runs.items():
        path = out / f"{name}.json"
        if path.exists():
            continue
        try:
            j = judge_dialogue(llm, args.model, lesson, run)
        except Exception as e:
            print("fail", name, e, flush=True)
            continue
        path.write_text(json.dumps({"run": name, "judge": args.model, **j}, ensure_ascii=False, indent=1))
        print("judged", name, flush=True)
    for name, run in runs.items():
        if run["system"] != "pipeline":
            continue
        other = name.replace("pipeline__", "baseline__")
        if other not in runs:
            continue
        for order in ("pb", "bp"):
            path = out / "pairwise" / f"{name.split('__', 1)[1]}__{order}.json"
            if path.exists():
                continue
            a, b = (run, runs[other]) if order == "pb" else (runs[other], run)
            try:
                r = compare(llm, args.model, lesson, a, b)
            except Exception as e:
                print("fail pair", name, e, flush=True)
                continue
            winner = {"A": "pipeline" if order == "pb" else "baseline", "B": "baseline" if order == "pb" else "pipeline", "tie": "tie"}
            mapped = {k: winner.get(v, v) for k, v in r.items() if k != "rationale"}
            path.write_text(json.dumps({"order": order, "judge": args.model, "result": mapped, "rationale": r.get("rationale", "")}, ensure_ascii=False, indent=1))
            print("pair", name, order, mapped.get("overall"), flush=True)


if __name__ == "__main__":
    main()
