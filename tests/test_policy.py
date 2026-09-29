from maieutica.analyzer import Analysis
from maieutica.learner import LearnerModel
from maieutica.lesson import load_lesson
from maieutica.policy import DialogueState, Policy

lesson = load_lesson("okr")


def fresh():
    lm = LearnerModel.for_lesson([c.id for c in lesson.concepts])
    return Policy(lesson), DialogueState(target=lesson.curriculum[0]), lm


def observe(lm, st, a):
    s = lm.concepts[st.target]
    if a.intent in ("answer", "dont_know", "ask_answer"):
        s.turns += 1
    if a.intent == "answer" and a.verdict != "not_applicable":
        s.covered |= set(a.covered_points)
        lm.observe(st.target, a.verdict, a.depth, s.hint_level)
    for e in a.evidence:
        if e["concept"] != st.target:
            lm.observe(e["concept"], e["verdict"], a.depth, 0)


def test_recall_with_misconception_starts_elenchus():
    pol, st, lm = fresh()
    a = Analysis(intent="answer", verdict="partially_correct", misconceptions=[{"id": "numbers_in_objective", "quote": "увеличить выручку на 20%"}])
    observe(lm, st, a)
    plan = pol.decide(st, lm, a)
    assert plan.move == "counterexample"
    assert plan.target == "objective"
    assert not plan.reveal_allowed


def test_ask_answer_escalates_then_bottoms_out():
    pol, st, lm = fresh()
    st.phase = "explore"
    moves = []
    for _ in range(4):
        a = Analysis(intent="ask_answer")
        observe(lm, st, a)
        moves.append(pol.decide(st, lm, a).move)
    assert moves[0] == "hint"
    assert "bottom_out" in moves
    assert moves.index("bottom_out") <= 3


def test_surface_correct_asks_why_then_advances():
    pol, st, lm = fresh()
    st.phase = "explore"
    a = Analysis(intent="answer", verdict="correct", depth=1, covered_points=[1, 2, 3])
    observe(lm, st, a)
    p1 = pol.decide(st, lm, a)
    assert p1.move == "probe_reasons"
    a2 = Analysis(intent="answer", verdict="correct", depth=2, covered_points=[1, 2, 3])
    observe(lm, st, a2)
    p2 = pol.decide(st, lm, a2)
    assert p2.move == "affirm_advance"
    assert p2.target == "objective"


def test_off_topic_redirects_without_state_change():
    pol, st, lm = fresh()
    st.phase = "explore"
    before = lm.concepts[st.target].p
    a = Analysis(intent="off_topic")
    plan = pol.decide(st, lm, a)
    assert plan.move == "redirect"
    assert lm.concepts[st.target].p == before


def test_frustration_adds_empathy():
    pol, st, lm = fresh()
    st.phase = "explore"
    a = Analysis(intent="dont_know", affect="frustrated")
    observe(lm, st, a)
    lm.affect.append("frustrated")
    plan = pol.decide(st, lm, a)
    assert plan.empathy
    assert "empathy" in plan.tags


def test_full_path_reaches_closing():
    pol, st, lm = fresh()
    phases = []
    for _ in range(60):
        if st.phase == "done":
            break
        a = Analysis(intent="answer", verdict="correct", depth=2, covered_points=[1, 2, 3, 4])
        observe(lm, st, a)
        plan = pol.decide(st, lm, a)
        phases.append(plan.phase)
    assert st.phase == "done"
    for p in ("explore", "apply", "reflect", "done"):
        assert p in phases


def test_stuck_learner_never_loops_forever():
    pol, st, lm = fresh()
    st.phase = "explore"
    for _ in range(80):
        if st.phase != "explore":
            break
        a = Analysis(intent="answer", verdict="incorrect", depth=0)
        observe(lm, st, a)
        pol.decide(st, lm, a)
    assert st.phase in ("apply", "reflect", "done")


def test_partial_answers_accumulate_coverage():
    pol, st, lm = fresh()
    st.phase = "explore"
    a1 = Analysis(intent="answer", verdict="partially_correct", depth=1, covered_points=[1])
    observe(lm, st, a1)
    assert pol.decide(st, lm, a1).move == "probe_clarify"
    a2 = Analysis(intent="answer", verdict="partially_correct", depth=2, covered_points=[2, 3])
    observe(lm, st, a2)
    plan = pol.decide(st, lm, a2)
    assert plan.move == "affirm_advance"


def test_no_question_repeats_within_concept():
    pol, st, lm = fresh()
    pol.open_plan(lm)
    st.phase = "explore"
    seen = []
    for _ in range(3):
        a = Analysis(intent="answer", verdict="partially_correct", depth=1, covered_points=[1])
        observe(lm, st, a)
        plan = pol.decide(st, lm, a)
        seen.append(plan.materials)
    qs = [m for m in seen if m]
    assert len(qs) == len(set(qs))
