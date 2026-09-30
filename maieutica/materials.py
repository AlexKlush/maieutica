"""Урок из любого текста.

build() собирает карту урока без нейросети: делит текст на смысловые части (по заголовкам, абзацам или группам
предложений), в каждой находит главные предложения и ключевые словосочетания (морфология — pymorphy3) и превращает
часть в понятие — с ожидаемым пониманием, ключевыми пунктами, вопросами и лестницей подсказок. Последнее понятие —
«Применение»: перенос на свою ситуацию. Такая карта грубее, чем от GigaChat (compiler.compile_lesson), но диалог по ней
идёт и без сети.

prepare() выбирает путь для приложения: текст → карта от GigaChat (если он доступен) или по правилам;
короткий запрос вроде «фотосинтез» → GigaChat сначала пишет короткий конспект по теме, потом строит по нему карту.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

from .lesson import Lesson
from .llm import GigaChat, LLMError
from .offline import MODEL as RULES, _SOCIAL

MAX_CHARS = 15000  # весь текст уходит в каждый запрос тьютора — длиннее будет медленно и дорого
MIN_CHARS = 240  # короче — это тема или вопрос, а не материал
MAX_PARTS = 6

_WORD = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё]*(?:-[A-Za-zА-Яа-яЁё]+)*")
_ABBR = {"т", "д", "п", "г", "гг", "в", "вв", "др", "пр", "см", "рис", "стр", "им", "ул", "ст", "руб", "млн", "млрд",
         "тыс", "кв", "напр", "е", "к", "н", "e", "i", "etc", "vs", "mr", "dr", "no"}
_BULLET = re.compile(r"^\s*(?:[-*•–—]|\d+[.)])\s+")
_IS = re.compile(r"^\s*(.{2,80}?)\s+[—–]\s+это\s")  # «X — это Y»
_DASH = re.compile(r"^\s*([^—–:.!?]{2,90}?)\s+[—–]\s+\S")  # «… хлорофилл — зелёный пигмент»
# Слова, из которых не выйдет названия части или подсказки: слишком общие.
_WEAK = frozenset(
    "роль часть время случай раз вид образ помощь счёт счет сторона мера ряд место пример момент вопрос ответ дело слово "
    "мысль текст автор человек люди год день неделя конец начало итог основа суть тема материал раздел глава".split()
)

GOAL = "Своими словами объяснить главные мысли текста и применить их к своей ситуации."
PROMISE = "К концу разговора вы сможете объяснить главное своими словами и применить это к своей ситуации."
APPLICATION_TASK = "Опишите, что бы вы сделали и какая мысль из текста за этим стоит."

OPEN_TERM = "Как бы вы своими словами объяснили, что такое {term}?"
OPEN = (
    "Что в тексте говорится {about} — как вы это поняли?",
    "Какая, по-вашему, главная мысль в части {about}?",
    "Как бы вы своими словами пересказали то, что сказано {about}?",
)
OPEN_PART = (  # название части — фраза («Почему зубрёжка обманывает»): его не склонить
    "О чём часть «{part}», если пересказать своими словами?",
    "Какая, по-вашему, главная мысль в части «{part}»?",
    "Как бы вы своими словами пересказали часть «{part}»?",
)
REASONS = (
    "Почему это так, как вы думаете: на чём держится эта мысль?",
    "Зачем это нужно, как вам кажется: что изменится, если этого не учитывать?",
    "Как вы это объясните: почему именно так, а не иначе?",
)
IMPLICATIONS = (
    "Где вы встречали это на практике — какой пример приходит в голову?",
    "Что из этого следует, если применить это к знакомой вам ситуации?",
    "С чем из того, что вы уже знаете, это связано?",
)


class NeedsText(ValueError):
    """Запрос нельзя превратить в урок: нужен текст (или GigaChat, чтобы написать конспект по теме)."""


# --- текст ------------------------------------------------------------------------------------------------------------

def clean(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ").replace("\t", " ")
    text = re.sub(r"[​﻿]", "", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)  # **жирный** из markdown
    lines = [re.sub(r"[ ]{2,}", " ", ln).rstrip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def sentences(text: str) -> list[str]:
    out, buf = [], ""
    for part in re.split(r"(?<=[.!?…])\s+", text.strip()):
        buf = f"{buf} {part}" if buf else part
        tail = re.search(r"([A-Za-zА-Яа-яЁё]+)\.$", buf)
        if tail and (tail.group(1).lower() in _ABBR or (len(tail.group(1)) == 1 and tail.group(1).isupper())):
            continue  # «т. е.», «А. С. Пушкин» — не конец предложения
        if buf.strip():
            out.append(buf.strip())
        buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def is_topic(text: str) -> bool:
    """Короткий запрос («фотосинтез», «хочу разобраться с производными») — это тема, а не материал для разбора."""
    t = clean(text)
    return len(t) < MIN_CHARS and len(sentences(t)) < 3


def is_chatter(text: str) -> bool:
    """«Привет», «ок», «?» — ни тема, ни материал."""
    t = clean(text).lower()
    return not t or bool(_SOCIAL.match(t)) or not any(len(w) >= 3 for w in _WORD.findall(t))


def _heading(line: str) -> str:
    s = line.strip()
    if _BULLET.match(s):
        return ""
    s = s.lstrip("#").strip().strip("*_").strip()
    if not s or len(s) > 90 or len(s.split()) > 11 or re.search(r"[.!?,;…]$", s):
        return ""
    return s.rstrip(":").strip()


def _paragraph(lines: list[str]) -> str:
    """Строки абзаца → текст. Пункты списка становятся отдельными предложениями."""
    out = []
    for ln in lines:
        s = ln.strip()
        if _BULLET.match(s):
            s = _BULLET.sub("", s)
            if s and not re.search(r"[.!?…:;]$", s):
                s += "."
        out.append(s)
    return re.sub(r"\s+", " ", " ".join(x for x in out if x)).strip()


def _sections(text: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """Заголовок всего текста (если первая строка похожа на заголовок) и части: (заголовок части, абзацы)."""
    if "\n\n" not in text:  # скопировано из браузера: абзацы разделены одним переводом строки
        text = text.replace("\n", "\n\n")
    lines = text.split("\n")
    title = ""
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is not None and _heading(lines[first]) and any(ln.strip() for ln in lines[first + 1:]):
        mark = re.match(r"\s*(#*)", lines[first]).group(1)
        same = mark and any(re.match(rf"\s*{mark}(?!#)\s*\S", ln) for ln in lines[first + 1:])
        if not same:  # «## Кривая забывания … ## Интервальные повторения» — это разделы, а не название всего текста
            title = _heading(lines[first])
            lines = lines[first + 1:]
    sections: list[tuple[str, list[str]]] = [("", [])]
    buf: list[str] = []

    def flush() -> None:
        if buf:
            p = _paragraph(buf)
            if p:
                sections[-1][1].append(p)
            buf.clear()

    for i, ln in enumerate(lines):
        if not ln.strip():
            flush()
            continue
        head = _heading(ln)
        nxt = next((x for x in lines[i + 1:] if x.strip()), "")
        if head and nxt and not _heading(nxt) and not buf:
            sections.append((head, []))
            continue
        buf.append(ln)
    flush()
    return title, [(h, ps) for h, ps in sections if ps]


def _units(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Смысловые части текста: (заголовок или "", текст части), не больше MAX_PARTS."""
    title, sections = _sections(text)
    if sum(1 for h, _ in sections if h) >= 2:
        units = [(h, " ".join(ps)) for h, ps in sections]
    else:
        units = [("", p) for _, ps in sections for p in ps]
    merged: list[tuple[str, str]] = []
    for h, body in units:  # мелкие абзацы (подпись, одна строка) приклеиваем к соседу
        if merged and not h and (len(body) < 140 or len(merged[-1][1]) < 140):
            merged[-1] = (merged[-1][0], f"{merged[-1][1]} {body}")
        else:
            merged.append((h, body))
    while len(merged) > MAX_PARTS:  # слишком дробно — объединяем самую короткую соседнюю пару
        i = min(range(len(merged) - 1), key=lambda k: len(merged[k][1]) + len(merged[k + 1][1]))
        (h1, b1), (h2, b2) = merged[i], merged[i + 1]
        merged[i:i + 2] = [(h1 or h2, f"{b1} {b2}")]
    if len(merged) == 1:  # один сплошной абзац — делим по предложениям
        h, body = merged[0]
        sents = sentences(body)
        k = max(1, min(4, len(sents) // 3))
        if k > 1:
            size = math.ceil(len(sents) / k)
            merged = [(h if i == 0 else "", " ".join(sents[i:i + size])) for i in range(0, len(sents), size)]
    return title, merged


# --- морфология: словосочетания и леммы ---------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _morph():
    try:
        import pymorphy3

        return pymorphy3.MorphAnalyzer()
    except Exception:  # без словаря карта грубее (слова в той форме, что в тексте), но собирается
        return None


@lru_cache(maxsize=50000)
def _parse(word: str, case: str = ""):
    """Самый вероятный разбор слова; case — предпочесть разбор в этом падеже (заголовки стоят в именительном)."""
    m = _morph()
    if not m:
        return None
    parses = m.parse(word)
    if case:
        for p in parses:
            if p.tag.case == case:
                return p
    return parses[0]


def _pos(p) -> str:
    if p is None:
        return "NOUN"
    if "LATN" in p.tag or "UNKN" in p.tag:
        return "NOUN"  # OKR, Key Results и прочие нерусские слова — как неизменяемые существительные
    return str(p.tag.POS or "")


def _lemma(w: str) -> str:
    p = _parse(w)
    if p is None or "LATN" in p.tag:
        return w.lower() if not w.isupper() else w
    return p.normal_form.replace("ё", "е")


def _modifier(p) -> bool:
    """Прилагательное или причастие, но не местоимённое («весь», «этот», «свой»)."""
    return p is not None and _pos(p) in ("ADJF", "PRTF") and "Apro" not in p.tag


def _agrees(word: str, head) -> bool:
    """Есть ли у слова разбор-прилагательное, согласованный с главным словом («световой» + «фазе»)."""
    m = _morph()
    if m is None or head is None:
        return False
    h = head.tag
    for p in m.parse(word):
        a = p.tag
        if _modifier(p) and a.case == h.case and a.number == h.number and (h.number == "plur" or a.gender in (None, h.gender)):
            return True
    return False


@dataclass
class Phrase:
    words: list[str]
    head: int  # индекс главного слова в words
    key: tuple[str, ...]  # леммы — чтобы считать одно словосочетание в разных падежах
    sent: int
    case: str
    spans: list[tuple[int, int]] = field(default_factory=list)
    where: float = 0.0  # место в предложении: 0 — начало, 1 — конец

    def nominative(self) -> str:
        """Словосочетание в именительном падеже: «индекса потребительских цен» → «индекс потребительских цен»."""
        return _decline(self.words, self.head, "nomn") or " ".join(self.words)


def _decline(words: list[str], head: int, case: str, given: str = "") -> str:
    """Слова группы с главным словом в нужном падеже (прилагательные перед ним — согласованно, хвост не меняется). "" — не склоняется.
    given — в каком падеже группа сейчас, если это известно (название части — в именительном)."""
    h = _parse(words[head], given)
    if h is None or "LATN" in h.tag or h.tag.POS != "NOUN":
        return ""
    if h.tag.case == case:
        return " ".join(words)
    number = h.tag.number or "sing"
    out = []
    for i, w in enumerate(words):
        p = _parse(w, given)
        if i <= head:
            if p is None or "LATN" in p.tag:
                return ""
            want = {case, number}
            if i < head and number == "sing" and h.tag.gender:
                want.add(h.tag.gender)
            if i < head and case == "accs" and (number == "plur" or h.tag.gender == "masc"):
                want.add("anim" if "anim" in h.tag else "inan")
            f = p.inflect(want)
            if f is None:
                return ""
            out.append(f.word[:1].upper() + f.word[1:] if w[:1].isupper() else f.word)
        else:
            out.append(w)
    return " ".join(out)


def _declinable(term: str) -> list[str] | None:
    """Слова названия, если это русская именная группа без скобок и тире (её можно склонять)."""
    if not re.fullmatch(r"[А-Яа-яЁё]+(?:[ -][А-Яа-яЁё]+){0,4}", term):
        return None
    words = term.split()
    tags = [_parse(w, "nomn") for w in words]
    if any(t is None for t in tags):
        return None
    head = next((i for i, t in enumerate(tags) if _pos(t) == "NOUN"), None)
    if head is None or any(not _modifier(t) for t in tags[:head]):
        return None
    return words


def _about(term: str) -> str:
    """«про фазу», «про индекс потребительских цен»; то, что не склоняется, — в кавычках."""
    words = _declinable(term)
    if words:
        head = next(i for i, w in enumerate(words) if _pos(_parse(w, "nomn")) == "NOUN")
        acc = _decline(words, head, "accs", "nomn")
        if acc:
            return "про " + _inline(acc)
    return f"про «{_inline(term)}»"


def _phrases(text: str) -> list[Phrase]:
    """Именные группы: [прилагательные] существительное [существительное в род. падеже с прилагательным]."""
    out: list[Phrase] = []
    for si, sent in enumerate(sentences(text)):
        toks = list(_WORD.finditer(sent))
        tags = [_parse(t.group(0)) for t in toks]
        gap = [True] + [re.fullmatch(r"\s+", sent[toks[i - 1].end():toks[i].start()]) is not None for i in range(1, len(toks))]
        for i, t in enumerate(toks):
            w = t.group(0)
            if _pos(tags[i]) != "NOUN" or (len(w) < 3 and not w.isupper()) or _lemma(w) in _WEAK:
                continue
            start = i
            while start > 0 and gap[start] and i - start < 2 and _agrees(toks[start - 1].group(0), tags[i]):
                start -= 1
            end, k = i, i + 1
            while k < len(toks) and gap[k] and end - i < 3:  # хвост в родительном падеже: «скорость фотосинтеза», «индекс потребительских цен»
                p = tags[k]
                if _modifier(p) and p.tag.case == "gent" and k + 1 < len(toks) and gap[k + 1] and _pos(tags[k + 1]) == "NOUN":
                    k += 1
                    continue
                if _pos(p) == "NOUN" and (p is None or "LATN" in p.tag or p.tag.case == "gent") and _lemma(toks[k].group(0)) not in _WEAK:
                    end, k = k, k + 1
                    continue
                break
            words = [x.group(0) for x in toks[start:end + 1]]
            head = tags[i]
            out.append(Phrase(words, i - start, tuple(_lemma(x) for x in words), si, str(head.tag.case or "") if head is not None else "",
                              [(x.start(), x.end()) for x in toks[start:end + 1]], start / max(1, len(toks))))
    return out


def _nouns(text: str) -> Counter:
    """Леммы существительных (для весов и подсказок)."""
    c: Counter = Counter()
    for t in _WORD.finditer(text):
        w = t.group(0)
        if _pos(_parse(w)) == "NOUN" and (len(w) >= 3 or w.isupper()) and _lemma(w) not in _WEAK:
            c[_lemma(w)] += 1
    return c


def _weights(units: list[str]) -> list[dict[str, float]]:
    """tf·idf лемм по частям: что характерно именно для этой части."""
    counts = [_nouns(u) for u in units]
    n = len(units)
    df = Counter(s for c in counts for s in c)
    return [{s: tf * (1.0 + math.log((1 + n) / (1 + df[s]))) for s, tf in c.items()} for c in counts]


def _cap(s: str) -> str:
    s = s.strip(" ,;:—–-«»\"")
    return s[:1].upper() + s[1:] if s else s


def _inline(term: str) -> str:
    """Название части внутри фразы: «хлорофилл», но «OKR» и «Эффект Даннинга» — как есть."""
    words = term.split()
    if not words or words[0][:2].isupper() or words[0].lower() == words[0]:
        return term
    p = _parse(words[0])
    if p is not None and any(g in p.tag for g in ("Name", "Surn", "Patr", "Geox", "Orgn", "Trad", "LATN")):
        return term
    return term[:1].lower() + term[1:]


def _defined(sents: list[str]) -> str:
    """Определяемое понятие: «X — это Y» или приложение «… хлорофилл — зелёный пигмент»."""
    for s in sents[:2]:
        m = _IS.match(s)
        if m and len(m.group(1).split()) <= 6:
            term = m.group(1).strip(" «»\"")
            return term.split("(")[0].strip() if len(term) > 40 and "(" in term else term
        m = _DASH.match(s)
        if m:
            left = m.group(1)
            if len(left.split()) <= 4 and not re.search(r"\b(это|есть|был|была|были)\b", left):
                return left.strip(" «»\"")
            ps = [p for p in _phrases(left) if p.spans and p.spans[-1][1] == len(left.rstrip())]
            if ps:  # «… играет хлорофилл — зелёный пигмент»: понятие стоит прямо перед тире
                return max(ps, key=lambda p: len(p.words)).nominative()
    return ""


def _title(body: str, weights: dict[str, float], taken: set[str]) -> str:
    """Название части: определяемое понятие или самое характерное словосочетание (в именительном падеже)."""
    term = _defined(sentences(body))
    if term and term.lower() not in taken:
        return _cap(term)
    phrases = _phrases(body)
    counts = Counter(p.key for p in phrases)
    options: dict[str, float] = {}
    for p in phrases:
        name = p.nominative()
        if name.lower() in taken or len(name) > 48:
            continue
        s = sum(weights.get(k, 0.0) for k in p.key) * (1 + 0.2 * (len(p.key) - 1))
        s *= (1 + 0.4 * (counts[p.key] - 1)) * (1 + 0.3 * (1 - p.where))
        if p.sent == 0:
            s *= 1.3 * (1.15 if p.case == "nomn" else 1.0)
        head = _parse(p.words[p.head])
        if head is not None and any(g in head.tag for g in ("Name", "Surn", "Patr")):
            s *= 0.3  # «Дэвид Даннинг» — кто открыл, а не что открыл
        options[name] = max(options.get(name, 0.0), s)
    # «В световой фазе … в темновой фазе» → «Световая и темновая фазы»
    by_head: dict[tuple[str, str], list[Phrase]] = {}
    for p in phrases:  # только относительные прилагательные («световая», не «высокая») в одном падеже — это виды одного и того же
        adj = _parse(p.words[0])
        known = adj is not None and len(adj.methods_stack) == 1  # словарное слово, а не догадка по окончанию
        if len(p.words) == 2 and p.head == 1 and adj is not None and not (known and "Qual" in adj.tag):
            by_head.setdefault((p.key[1], p.case), []).append(p)
    for (lemma, _case), ps in by_head.items():
        mods = list(dict.fromkeys(p.key[0] for p in ps))
        if len(mods) == 2:
            head = _parse(ps[0].words[1])
            plural = head.inflect({"nomn", "plur"}) if head is not None else None
            adjs = [_parse(p.words[0]).inflect({"nomn", "sing", head.tag.gender} if head.tag.gender else {"nomn", "sing"})
                    for p in ps if p.key[0] in mods][:2] if head is not None else []
            names = list(dict.fromkeys(a.word for a in adjs if a))
            if plural and len(names) == 2:
                options[f"{names[0]} и {names[1]} {plural.word}"] = sum(options.get(p.nominative(), 0.0) for p in ps) * 1.2
    best = max(options.items(), key=lambda kv: kv[1], default=("", 0.0))[0]
    return _cap(best)


def _trim(s: str, limit: int) -> str:
    s = s.strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for sep in ("; ", ", ", " — ", " "):
        k = cut.rfind(sep)
        if k > limit * 0.55:
            return cut[:k].rstrip(" ,;—") + "…"
    return cut.rstrip() + "…"


def _key_sentences(text: str, weights: dict[str, float], n: int = 3) -> list[str]:
    sents = [s for s in sentences(text) if len(s.split()) >= 4] or sentences(text)

    def score(i: int, s: str) -> float:
        lemmas = set(_nouns(s))
        base = sum(weights.get(x, 0.0) for x in lemmas) / (1.0 + math.sqrt(len(lemmas) or 1))
        if i == 0:
            base *= 1.25
        if _IS.match(s) or re.search(r"\b(это|означает|называ\w+|является)\b", s):
            base *= 1.2
        return base

    ranked = sorted(range(len(sents)), key=lambda i: -score(i, sents[i]))[:n]
    return [sents[i] for i in sorted(ranked)]


# --- карта урока ------------------------------------------------------------------------------------------------------

def _display(lemma: str, text: str) -> str:
    """Слово для подсказки в именительном падеже и в том числе, как в тексте («денег» → «деньги»). "" — имя или отчество."""
    for t in _WORD.finditer(text):
        w = t.group(0)
        if _lemma(w) != lemma:
            continue
        p = _parse(w)
        if p is None or "LATN" in p.tag or w.isupper():
            return w
        if any(g in p.tag for g in ("Name", "Patr")):
            return ""
        f = p.inflect({"nomn", p.tag.number or "sing"})
        word = f.word if f else w
        return word[:1].upper() + word[1:] if "Surn" in p.tag or "Geox" in p.tag else word
    return ""


def _lead(sentence: str) -> str:
    """Начало мысли для подсказки: примерно первая половина предложения, без предлога или союза на конце."""
    words = sentence.split()
    cut = words[: max(3, int(len(words) * 0.45))]
    while len(cut) > 2:
        p = _parse(cut[-1].strip(",;:—–-"))
        if len(cut[-1]) <= 2 or (p is not None and p.tag.POS in ("PREP", "CONJ", "PRCL")):
            cut.pop()
        else:
            break
    return " ".join(cut).rstrip(" ,;:—–-")


def _short(term: str) -> str:
    """Короткая подпись для прогресса: без пояснения в скобках, не длиннее 26 знаков."""
    t = re.sub(r"\s*\([^)]*\)", "", term).strip() or term
    return t if len(t) <= 26 else _trim(t, 24)


def open_question(i: int, term: str) -> str:
    """Открытый вопрос к части с таким названием (для карт, где модель вопрос не прислала)."""
    return _open(i, term, False, _declinable(term) is not None)


def _open(i: int, term: str, defined: bool, declinable: bool) -> str:
    quoted = _inline(term) if declinable else f"«{_inline(term)}»"
    if defined:
        return OPEN_TERM.format(term=quoted)
    if declinable or re.fullmatch(r"[A-Za-z0-9 ()+&-]+", term):
        return OPEN[i % len(OPEN)].format(about=_about(term))
    return OPEN_PART[i % len(OPEN_PART)].format(part=term)


def _concept(i: int, head: str, body: str, weights: dict[str, float], taken: set[str]) -> dict:
    key = _key_sentences(body, weights)
    main = key[0] if key else body
    term = head or _title(body, weights, taken) or f"Часть {i + 1}"
    if term.lower() in taken:
        term = f"{term}, часть {i + 1}"
    taken.add(term.lower())
    in_text = _inline(term)
    defined = not head and _defined(sentences(body)).lower() == term.lower()
    declinable = _declinable(term) is not None
    words = []
    for lemma in sorted(weights, key=lambda k: -weights[k]):
        w = _display(lemma, body)
        if w and lemma not in term.lower() and w.lower() not in term.lower():
            words.append(w)
        if len(words) == 2:
            break
    lead = _lead(main)
    hints = [
        (f"Посмотрите ещё раз на эту часть текста и обратите внимание на слова «{words[0]}» и «{words[1]}». Как они связаны между собой?"
         if len(words) == 2 else f"Посмотрите ещё раз на эту часть текста: что в ней сказано {_about(term)}?"),
        f"Подскажу, с чего начинается мысль в тексте: «{lead}…» Как бы вы её продолжили?",
        f"В тексте сказано: «{_trim(main, 260).replace('?', '.')}» Как бы вы сказали это своими словами?",
    ]
    return {
        "id": f"part{i + 1}",
        "title": term,
        "short": _short(term),
        "bloom": "understand",
        "expectation": _trim(" ".join(key[:2]) if len(key) > 1 and len(key[0]) < 90 else main, 320),
        "key_points": [_trim(s, 170) for s in key] or [_trim(body, 170)],
        "questions": {
            "open": _open(i, term, defined, declinable),
            "reasons": REASONS[i % len(REASONS)],
            "implications": IMPLICATIONS[i % len(IMPLICATIONS)],
        },
        "hints": hints,
        "source_span": _trim(body, 200),
    }


def application() -> dict:
    return {
        "id": "application",
        "title": "Применение",
        "short": "Практика",
        "bloom": "create",
        "weight": 1.2,
        "expectation": "Ученик переносит идеи материала на свою ситуацию: называет, что конкретно сделает, и объясняет, какая мысль из текста за этим стоит.",
        "key_points": ["опирается на конкретную мысль из материала", "объясняет, почему поступит именно так", "описывает конкретную ситуацию или шаг"],
        "questions": {
            "open": "Где бы вы могли применить мысли из этого текста — в учёбе, работе или жизни?",
            "evaluate": "Что в вашем решении самое слабое место и как его усилить?",
        },
        "hints": [
            "Выберите одну мысль из текста, которая показалась вам самой полезной. Где она пригодилась бы вам?",
            "Представьте конкретную ситуацию на этой неделе: что бы вы сделали иначе, зная этот материал?",
            "Опишите одну ситуацию и один шаг: что именно вы сделаете и какая мысль из текста за этим стоит?",
        ],
        "source_span": "весь текст",
    }


def fit(text: str) -> tuple[str, bool]:
    """Текст для урока и был ли он обрезан до MAX_CHARS (по границе абзаца или предложения)."""
    text = clean(text)
    if len(text) <= MAX_CHARS:
        return text, False
    cut = text[:MAX_CHARS]
    k = max(cut.rfind("\n\n"), cut.rfind(". "))
    return (cut[: k + 1] if k > MAX_CHARS * 0.7 else cut).rstrip(), True


def lesson_id(text: str) -> str:
    return "text-" + hashlib.sha1(text.encode()).hexdigest()[:8]


def title_of(text: str) -> str:
    """Название материала: заголовок, определяемое понятие первой фразы или главное словосочетание текста."""
    head, units = _units(clean(text))
    if head:
        return _trim(head, 80)
    body = " ".join(b for _, b in units) or clean(text)
    term = _defined(sentences(body))
    if term and len(term) <= 60:
        return _cap(term)
    return _title(body, {k: float(v) for k, v in _nouns(body).items()}, set()) or _trim(sentences(body)[0], 60)


def build(text: str, title: str = "") -> Lesson:
    """Карта урока по тексту без нейросети. NeedsText — если текст слишком короткий для разбора."""
    text, _ = fit(text)
    if len(text) < MIN_CHARS or len(sentences(text)) < 2:
        raise NeedsText("Текст слишком короткий: для разбора нужно хотя бы несколько предложений.")
    title = title or title_of(text)
    _, units = _units(text)
    weights = _weights([b for _, b in units])
    taken = {"применение", "практика"}
    concepts = [_concept(i, h, b, weights[i], taken) for i, (h, b) in enumerate(units)]
    concepts.append(application())
    return Lesson.model_validate({
        "id": lesson_id(text),
        "title": title,
        "source_text": text,
        "goal": GOAL,
        "promise": PROMISE,
        "application_task": APPLICATION_TASK,
        "concepts": concepts,
        "curriculum": [c["id"] for c in concepts],
    })


# --- тема → конспект → урок (GigaChat) --------------------------------------------------------------------------------

WRITER = """Ты — автор учебных конспектов. По запросу ученика напиши короткий ясный конспект для самостоятельного чтения: 180–280 слов, 3–5 абзацев, первая строка — короткий заголовок темы (без точки), дальше абзацы через пустую строку. Начни с определения, затем — как это устроено и почему, в конце — где это встречается на практике. Только общепринятые проверенные факты, без выдуманных цифр, имён и дат. Без списков, markdown, эмодзи и обращений к читателю. Пиши по-русски."""


def write_material(llm: GigaChat, request: str, model: str, timeout: float | None = None) -> str:
    res = llm.chat([{"role": "system", "content": WRITER}, {"role": "user", "content": f"Запрос ученика: {request.strip()[:300]}"}],
                   model=model, temperature=0.3, max_tokens=900, timeout=timeout)
    text = clean(re.sub(r"[*#]+", "", res.content))
    if len(text) < MIN_CHARS:
        raise LLMError("GigaChat вернул слишком короткий конспект")
    return text


@dataclass
class Prepared:
    lesson: Lesson
    built_by: str  # модель GigaChat, которая собрала карту, или offline.MODEL
    generated: bool = False  # материал написан GigaChat по теме, а не вставлен
    truncated: bool = False
    note: str = ""  # почему карта собрана без нейросети


def prepare(request: str, llm: GigaChat | None, model: str, notify: Callable[[str], None] | None = None, timeout: float = 150.0) -> Prepared:
    """Урок по запросу: текст → карта (GigaChat или правила), тема → конспект от GigaChat → карта. Бросает NeedsText."""
    from .compiler import compile_lesson  # compiler сам импортирует этот модуль

    say = notify or (lambda key: None)
    request = clean(request)
    if is_chatter(request):
        raise NeedsText("Назовите тему или вставьте текст, который хотите разобрать.")
    generated = False
    if is_topic(request):
        if llm is None or not llm.available():
            why = "GigaChat не подключён" if llm is None else "GigaChat сейчас недоступен"
            raise NeedsText(f"{why}, поэтому конспект по теме написать не могу. Вставьте текст для разбора — от пары абзацев.")
        say("write")
        try:
            text = write_material(llm, request, model, timeout=timeout)
        except LLMError as e:
            raise NeedsText(f"Не получилось подготовить конспект: {e}. Попробуйте ещё раз или вставьте свой текст.") from e
        generated = True
    else:
        text = request
    text, truncated = fit(text)
    if llm is not None and llm.available():
        say("map")
        try:
            lesson, used = compile_lesson(llm, text, model=model, timeout=timeout)
            return Prepared(lesson, used or model, generated, truncated)
        except LLMError as e:
            note = f"карта урока собрана по правилам: GigaChat не справился ({e})"
    elif llm is None:
        note = "GigaChat не подключён — карта урока собрана по правилам"
    else:
        note = f"GigaChat недоступен ({llm.last_error or 'пауза после сбоя'}) — карта урока собрана по правилам"
    say("rules")
    return Prepared(build(text), RULES, generated, truncated, note)
