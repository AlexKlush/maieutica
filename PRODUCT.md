# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Static HTML/CSS/JS page on GitHub Pages (confirmed: user chose GitHub Pages) presenting the work, with the live chat demo as a Streamlit app on Streamlit Community Cloud embedded in the page (iframe) and linked separately. Backend: Python pipeline calling the GigaChat API (freemium, 1 concurrent stream). Libraries for the page: delegated to the builder (vanilla JS + GSAP/Lenis-class motion libraries from cdnjs/jsdelivr are acceptable).

## Users

Primary: ML/LLM specialists at a Russian company reviewing internship test assignments (Data Scientist / LLM Engineer). They open the link during screening and at the interview demo, skim the reasoning, poke the live chat with tricky learner replies (wrong answers, "just tell me the answer", off-topic, frustration), and judge research depth, pipeline design, engineering quality and honesty of evaluation. Secondary: the candidate (Алексей Клушин) demoing it live at the interview.

## Product Purpose

«Росток» (formerly «Майевтика») is a Socratic-dialogue tutor for any material: the learner pastes a text (or names a topic and GigaChat writes a short study note), and the system runs a structured Socratic dialogue that helps the learner practise and master the material: empathic, pushes the learner to think, goal-oriented, grounded in recent research. Success: reviewers see a systematic, research-backed dialogue (not chit-chat), can try it themselves, and understand why each design choice was made.

## Positioning

Not "one clever prompt": a multi-stage pipeline in which the LLM does perception and wording while an explicit, inspectable pedagogical policy decides what to do: diagnosis of the learner's reply (intent, correctness, misconceptions, affect) → Bayesian knowledge tracing over the lesson's concepts → rule-based choice of a Socratic move (elenchus counterexample, graduated hints, self-explanation probes, transfer case) → generation under a hidden tutor constitution → verifier that blocks answer leakage and false praise. Every turn is traceable in the UI.

## Operating Context

The test assignment (ТЗ) requires: research in verified sources (arXiv), with emphasis on the most recent work; analysis of the most effective practices and why; a pipeline built on that research; a chat demo (e.g. Streamlit) with justification of the logic; demonstration on the given OKR text. GigaChat API freemium is the prescribed LLM. The page is read on laptops during screening and shown on a shared screen at the interview.

## Capabilities and Constraints

- Language: Russian (UI, dialogue, page copy); research citations keep original English titles.
- GigaChat freemium: ample tokens, but one concurrent request per key; the app queues calls and retries on 429. Streamlit Community Cloud apps sleep when idle and must be woken.
- Lessons are data (JSON knowledge map: concepts, key points, misconceptions, hints, cases); OKR lesson hand-curated; other texts can be compiled by the pipeline.
- Evaluation results shown on the page must come from actual runs (simulated-student personas + LLM judges); no invented numbers.

## Brand Commitments

Name: «Росток» («sprout»: a thought that grows from a question; the method is still maieutics, Socrates' "midwifery" of ideas). The OKR text of the assignment is one example among others, not the product's subject. Author credit: Алексей Клушин, GitHub AlexKlush. Public repository.

## Evidence on Hand

- ТЗ text and OKR source text (lessons/okr.json).
- Verified literature review in research/ (REPORT.md, papers.json, practices.json) — being produced.
- Live pipeline and its per-turn traces; evaluation runs (to be produced). Do not fabricate testimonials, benchmark numbers, or usage claims.

## Product Principles

1. Show the mechanism, not adjectives: every claim on the page is backed by a trace, a paper, or a measured number.
2. Research → decision → component: each design decision links to its evidence.
3. Honest evaluation, including limitations and failure cases.
4. The learner does the thinking; the system never lectures by default.
