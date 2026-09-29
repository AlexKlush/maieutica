from __future__ import annotations

import argparse
import json
import re
import statistics as stats
from collections import Counter, defaultdict
from pathlib import Path

from .personas import PERSONAS

EVAL = Path(__file__).resolve().parent
ROOT = EVAL.parent
TURN_DIMS = ["mistake_identification", "mistake_location", "revealing_answer", "providing_guidance", "actionability", "coherence", "humanlike", "empathy"]
DLG_DIMS = ["learning_progress", "socratic_quality", "empathy", "goal_orientation", "robustness"]
SCORE = {"yes": 1.0, "partly": 0.5, "no": 0.0}


def ends_with_one_question(text: str) -> bool:
    return text.count("?") == 1 and bool(re.search(r"\?[»\"')\s]*$", text.strip()))


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(stats.mean(xs), 3) if xs else None


def sd(xs):
    xs = [x for x in xs if x is not None]
    return round(stats.stdev(xs), 3) if len(xs) > 1 else 0.0


def spearman(a, b):
    if len(a) < 3:
        return None
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    ma, mb = stats.mean(ra), stats.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return round(num / den, 3) if den else None


def load(tag: str):
    runs = {p.stem: json.loads(p.read_text()) for p in sorted((EVAL / "runs" / tag).glob("*.json"))}
    judges = {}
    jdir = EVAL / "judgments" / tag
    if jdir.exists():
        for d in sorted(jdir.iterdir()):
            if d.is_dir():
                judges[d.name] = {
                    "single": {p.stem: json.loads(p.read_text()) for p in d.glob("*.json")},
                    "pairwise": {p.stem: json.loads(p.read_text()) for p in (d / "pairwise").glob("*.json")} if (d / "pairwise").exists() else {},
                }
    return runs, judges


def system_metrics(runs: dict, judge: dict | None, system: str) -> dict:
    rs = {k: v for k, v in runs.items() if v["system"] == system}
    tutor_turns = [m for r in rs.values() for m in r["dialogue"][1:] if m["role"] == "tutor"]
    out = {
        "dialogues": len(rs),
        "tutor_turns": len(tutor_turns),
        "one_question_rate": mean([1.0 if ends_with_one_question(m["content"]) else 0.0 for m in tutor_turns]),
        "words_per_turn": mean([len(m["content"].split()) for m in tutor_turns]),
        "latency_per_turn": mean([m.get("latency") for m in tutor_turns]),
        "tokens_per_turn": mean([m.get("tokens") for m in tutor_turns]),
    }
    if system == "pipeline":
        out["regenerate_rate"] = mean([1.0 if m.get("regenerated") else 0.0 for m in tutor_turns])
        out["fallback_rate"] = mean([1.0 if m.get("fallback") else 0.0 for m in tutor_turns])
        out["moves"] = Counter(m.get("move_label", m.get("move")) for m in tutor_turns).most_common()
        out["finished_rate"] = mean([1.0 if r.get("finished") else 0.0 for r in rs.values()])
        stage_lat = defaultdict(list)
        for m in tutor_turns:
            for s in m.get("stages", []):
                if s.get("latency"):
                    stage_lat[s["label"]].append(s["latency"])
        out["stage_latency"] = {k: mean(v) for k, v in stage_lat.items()}
        out["final_mastery"] = mean([mean(list(r.get("final_mastery", {}).values())) for r in rs.values()])
    if judge:
        js = [judge["single"][k] for k in rs if k in judge["single"]]
        turns = [t for j in js for t in j.get("turns", [])]
        for d in TURN_DIMS:
            vals = [SCORE[t[d]] for t in turns if t.get(d) in SCORE]
            out[f"turn_{d}"] = mean(vals)
            out[f"turn_{d}_n"] = len(vals)
        out["tone_encouraging"] = mean([1.0 if t.get("tutor_tone") == "encouraging" else 0.0 for t in turns])
        out["tone_offensive"] = sum(1 for t in turns if t.get("tutor_tone") == "offensive")
        for d in DLG_DIMS:
            vals = [j.get(d) for j in js if isinstance(j.get(d), int)]
            out[f"dlg_{d}"] = mean(vals)
            out[f"dlg_{d}_sd"] = sd(vals)
        out["concepts_covered"] = mean([len(set(j.get("concepts_covered", []))) for j in js])
    return out


def pairwise(judge: dict) -> dict:
    by_key = defaultdict(dict)
    for name, r in judge["pairwise"].items():
        key, order = name.rsplit("__", 1)
        by_key[key][order] = r["result"]
    crit = DLG_DIMS + ["overall"]
    res = {c: Counter() for c in crit}
    per_persona = defaultdict(lambda: Counter())
    for key, orders in by_key.items():
        if len(orders) < 2:
            continue
        for c in crit:
            a, b = orders["pb"].get(c), orders["bp"].get(c)
            outcome = a if a == b else "tie"
            res[c][outcome] += 1
            if c == "overall":
                per_persona[key.split("__")[0]][outcome] += 1
    return {"criteria": {c: dict(v) for c, v in res.items()}, "per_persona": {k: dict(v) for k, v in per_persona.items()}, "pairs": len(by_key)}


def agreement(runs: dict, judges: dict) -> dict:
    if len(judges) < 2:
        return {}
    names = sorted(judges)
    a, b = judges[names[0]]["single"], judges[names[1]]["single"]
    common = sorted(set(a) & set(b))
    out = {"judges": names, "dialogues": len(common)}
    for d in DLG_DIMS:
        xa = [a[k].get(d) for k in common]
        xb = [b[k].get(d) for k in common]
        pairs = [(x, y) for x, y in zip(xa, xb) if isinstance(x, int) and isinstance(y, int)]
        out[d] = spearman([p[0] for p in pairs], [p[1] for p in pairs]) if pairs else None
    reveal_a, reveal_b = [], []
    for k in common:
        ta = {t["turn"]: t for t in a[k].get("turns", [])}
        tb = {t["turn"]: t for t in b[k].get("turns", [])}
        for n in set(ta) & set(tb):
            if ta[n].get("revealing_answer") in ("yes", "no") and tb[n].get("revealing_answer") in ("yes", "no"):
                reveal_a.append(ta[n]["revealing_answer"])
                reveal_b.append(tb[n]["revealing_answer"])
    if reveal_a:
        out["revealing_answer_agreement"] = round(sum(x == y for x, y in zip(reveal_a, reveal_b)) / len(reveal_a), 3)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="main")
    ap.add_argument("--out", default=str(ROOT / "docs" / "data" / "eval.json"))
    args = ap.parse_args()
    runs, judges = load(args.tag)
    result = {"tag": args.tag, "personas": PERSONAS, "judges": {}, "systems": sorted({r["system"] for r in runs.values()})}
    for jname, j in judges.items():
        result["judges"][jname] = {
            "systems": {s: system_metrics(runs, j, s) for s in result["systems"]},
            "pairwise": pairwise(j) if j["pairwise"] else None,
            "per_persona": {
                p["id"]: {s: {d: (j["single"].get(f"{s}__{p['id']}__0", {}) or {}).get(d) for d in DLG_DIMS} for s in result["systems"]}
                for p in PERSONAS
            },
        }
    if not judges:
        result["judges"]["none"] = {"systems": {s: system_metrics(runs, None, s) for s in result["systems"]}}
    result["agreement"] = agreement(runs, judges)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "personas"}, ensure_ascii=False, indent=1)[:6000])


if __name__ == "__main__":
    main()
