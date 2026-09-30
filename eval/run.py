from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

from maieutica import GigaChat, load_lesson
from maieutica.engine import Tutor
from maieutica.generator import opening
from maieutica.llm import LLMError

from .personas import PERSONAS

RUNS = Path(__file__).resolve().parent / "runs"

STUDENT_SYSTEM = """{persona}

Ситуация: ты недавно прочитал учебный текст про OKR и теперь переписываешься в чате с тьютором. Пиши ТОЛЬКО свою следующую реплику — от первого лица, без кавычек, пояснений и ремарок. Не будь умнее своей персоны: не пересказывай текст целиком и не цитируй его дословно. Никогда не упоминай, что ты персонаж или ИИ. Реплика — одно–три предложения.

{memory}"""

MEMORY_FULL = "ТЕКСТ, КОТОРЫЙ ТЫ ПРОЧИТАЛ (помнишь его хорошо):\n{text}"
MEMORY_PARTIAL = "ЧТО ТЫ ЗАПОМНИЛ ИЗ ТЕКСТА (это всё, что у тебя в голове; самого текста перед тобой нет, и твои воспоминания могут быть ошибочными — ты об этом не знаешь). Новое понимание появляется у тебя только из разговора с тьютором:\n{memory}"

BASELINE_SYSTEM = """Ты — сократический тьютор. Ученик только что изучил текст ниже. Проведи сократический диалог, который поможет ему потренировать и освоить материал: задавай вопросы, подталкивай к размышлению, не давай готовых ответов, проявляй эмпатию и держи фокус на цели — освоить материал. Отвечай кратко, на «вы».

ТЕКСТ:
{text}"""


def student_reply(llm: GigaChat, model: str, persona: dict, text: str, dialogue: list[dict]) -> str:
    scripted = persona.get("scripted")
    n = sum(1 for m in dialogue if m["role"] == "student")
    if scripted and n % 2 == 1:
        return scripted[(n // 2) % len(scripted)]
    memory = MEMORY_FULL.format(text=text) if persona.get("memory", "full") == "full" else MEMORY_PARTIAL.format(memory=persona["memory"])
    msgs = [{"role": "system", "content": STUDENT_SYSTEM.format(persona=persona["prompt"], memory=memory)}]
    for m in dialogue:
        msgs.append({"role": "user" if m["role"] == "tutor" else "assistant", "content": m["content"]})
    res = llm.chat(msgs, model=model, temperature=0.9, top_p=0.95, max_tokens=200)
    return res.content.strip().strip('"«»')


def run_pipeline(llm: GigaChat, lesson, persona: dict, turns: int, student_model: str, models: dict) -> dict:
    tutor = Tutor(lesson, llm, models, allow_offline=False)  # в оценке сбой LLM не подменяем правилами
    first = tutor.start()
    dialogue = [{"role": "tutor", "content": first.reply}]
    for _ in range(turns):
        s = student_reply(llm, student_model, persona, lesson.source_text, dialogue)
        dialogue.append({"role": "student", "content": s})
        tr = tutor.step(s)
        dialogue.append({"role": "tutor", "content": tr.reply, "move": tr.move, "move_label": tr.move_label, "phase": tr.phase, "target": tr.target,
                         "latency": tr.total_latency, "tokens": tr.tokens, "regenerated": tr.verifier.get("regenerated", False), "fallback": tr.verifier.get("fallback", False),
                         "analysis": tr.analysis, "stages": [asdict(x) for x in tr.stages]})
        if tutor.finished:
            break
    return {"dialogue": dialogue, "final_mastery": tutor.export()["final_mastery"], "finished": tutor.finished, "phase": tutor.state.phase}


def run_baseline(llm: GigaChat, lesson, persona: dict, turns: int, student_model: str, model: str) -> dict:
    system = BASELINE_SYSTEM.format(text=lesson.source_text)
    dialogue = [{"role": "tutor", "content": opening(lesson)}]
    for _ in range(turns):
        s = student_reply(llm, student_model, persona, lesson.source_text, dialogue)
        dialogue.append({"role": "student", "content": s})
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": "(начало занятия)"}]
        for m in dialogue:
            role = "assistant" if m["role"] == "tutor" else "user"
            msgs.append({"role": role, "content": m["content"]})
        t0 = time.perf_counter()
        res = llm.chat(msgs, model=model, temperature=0.55, top_p=0.9, max_tokens=450)
        dialogue.append({"role": "tutor", "content": res.content.strip(), "latency": round(time.perf_counter() - t0, 2), "tokens": res.usage.total})
    return {"dialogue": dialogue}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", nargs="+", default=["pipeline", "baseline"])
    ap.add_argument("--personas", nargs="+", default=[p["id"] for p in PERSONAS])
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--turns", type=int, default=12)
    ap.add_argument("--student-model", default="GigaChat-2-Pro")
    ap.add_argument("--analyzer", default="GigaChat-2-Max")
    ap.add_argument("--generator", default="GigaChat-2-Max")
    ap.add_argument("--verifier", default="GigaChat-2-Max")
    ap.add_argument("--tag", default="main")
    args = ap.parse_args()

    llm = GigaChat()
    lesson = load_lesson("okr")
    out_dir = RUNS / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    models = {"analyzer": args.analyzer, "generator": args.generator, "verifier": args.verifier}
    for seed in range(args.seeds):
        for pid in args.personas:
            persona = next(p for p in PERSONAS if p["id"] == pid)
            for system in args.systems:
                path = out_dir / f"{system}__{pid}__{seed}.json"
                if path.exists():
                    continue
                t0 = time.perf_counter()
                for attempt in range(3):
                    try:
                        if system == "pipeline":
                            res = run_pipeline(llm, lesson, persona, args.turns, args.student_model, models)
                        else:
                            res = run_baseline(llm, lesson, persona, args.turns, args.student_model, args.generator)
                        break
                    except LLMError as e:
                        print("retry", system, pid, e, flush=True)
                        time.sleep(10)
                else:
                    continue
                res.update({"system": system, "persona": pid, "seed": seed, "models": models if system == "pipeline" else {"generator": args.generator},
                            "student_model": args.student_model, "wall": round(time.perf_counter() - t0, 1)})
                path.write_text(json.dumps(res, ensure_ascii=False, indent=1))
                print(f"done {system} {pid} seed={seed} turns={len(res['dialogue']) // 2} {res['wall']}s", flush=True)


if __name__ == "__main__":
    main()
