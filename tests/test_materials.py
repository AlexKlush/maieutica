import pytest

from maieutica import materials, verifier
from maieutica.engine import Tutor
from maieutica.llm import GigaChat
from maieutica.offline import MODEL as RULES
from maieutica.policy import Plan

from .fake_gigachat import FakeGigaChat

PHOTO = """Фотосинтез

Фотосинтез — это процесс, при котором растения, водоросли и некоторые бактерии превращают энергию света в энергию химических связей. Из углекислого газа и воды они синтезируют глюкозу, а в качестве побочного продукта выделяют кислород.

Главную роль в фотосинтезе играет хлорофилл — зелёный пигмент, который находится в хлоропластах. Хлорофилл поглощает в основном красный и синий свет, а зелёный отражает, поэтому листья кажутся нам зелёными.

Фотосинтез идёт в две стадии. В световой фазе энергия света расщепляет молекулы воды, при этом выделяется кислород и запасается энергия в виде АТФ. В темновой фазе, которая не требует света напрямую, эта энергия используется, чтобы связать углекислый газ и построить глюкозу.

Скорость фотосинтеза зависит от освещённости, концентрации углекислого газа и температуры. Если один из этих факторов в недостатке, он ограничивает весь процесс, даже когда остальных хватает с избытком."""

SECTIONS = """## Кривая забывания
Герман Эббингауз заучивал бессмысленные слоги и проверял, сколько из них помнит спустя разное время. Большая часть нового забывается в первые часы и дни, а потом забывание замедляется.

## Интервальные повторения
Если повторять материал через растущие промежутки времени, кривая забывания каждый раз становится более пологой. На этом принципе построены карточки для заучивания слов.

## Активное припоминание
Повторение работает лучше, если не перечитывать конспект, а вспоминать материал самому: отвечать на вопросы, пересказывать, решать задачи."""


def check_lesson(lesson):
    ids = [c.id for c in lesson.concepts]
    assert len(ids) == len(set(ids)) and 2 <= len(ids) <= 7
    assert lesson.curriculum[-1] == "application" and lesson.concept("application").bloom == "create"
    for c in lesson.concepts:
        assert c.title and c.short and len(c.short) <= 26 and c.expectation and c.key_points
        assert len(c.hints) == 3 and all(h.count("?") == 1 and h.rstrip().endswith("?") for h in c.hints), c.hints
        assert all(q.count("?") == 1 and q.endswith("?") for q in c.questions.values()), c.questions


def test_build_from_paragraphs():
    lesson = materials.build(PHOTO)
    check_lesson(lesson)
    assert lesson.title == "Фотосинтез"
    titles = [c.title for c in lesson.concepts]
    assert titles[:2] == ["Фотосинтез", "Хлорофилл"]  # «X — это …» и «… хлорофилл — зелёный пигмент»
    assert lesson.concept("part1").questions["open"] == "Как бы вы своими словами объяснили, что такое фотосинтез?"


def test_build_from_markdown_sections_uses_headings():
    lesson = materials.build(SECTIONS)
    check_lesson(lesson)
    assert [c.title for c in lesson.concepts[:-1]] == ["Кривая забывания", "Интервальные повторения", "Активное припоминание"]
    assert "про интервальные повторения" in lesson.concept("part2").questions["open"]
    assert lesson.concept("part1").questions["open"] == "Что в тексте говорится про кривую забывания — как вы это поняли?"


def test_single_paragraph_is_split_and_browser_copy_is_paragraphs():
    one = " ".join(PHOTO.split("\n\n")[1:])
    check_lesson(materials.build(one))
    lines = "\n".join(PHOTO.split("\n\n"))  # абзацы через один перевод строки, как при копировании со страницы
    assert len(materials.build(lines).concepts) == len(materials.build(PHOTO).concepts)


def test_topic_chatter_and_short_text():
    assert materials.is_topic("фотосинтез") and materials.is_topic("Хочу разобраться с производными")
    assert not materials.is_topic(PHOTO)
    assert materials.is_chatter("привет") and materials.is_chatter("?") and not materials.is_chatter("инфляция")
    with pytest.raises(materials.NeedsText):
        materials.build("Коротко. Очень.")


def test_prepare_offline_text_and_topic():
    ready = materials.prepare(PHOTO, None, "GigaChat-2-Max")
    assert ready.built_by == RULES and "не подключён" in ready.note and not ready.generated
    with pytest.raises(materials.NeedsText, match="Вставьте текст"):
        materials.prepare("фотосинтез", None, "GigaChat-2-Max")
    with pytest.raises(materials.NeedsText):
        materials.prepare("привет", None, "GigaChat-2-Max")


def test_prepare_with_gigachat_builds_map_and_writes_material_for_topic():
    fake = FakeGigaChat()
    llm = GigaChat("ZmFrZTpmYWtl", transport=__import__("httpx").MockTransport(fake), substitute_models=True)
    stages = []
    ready = materials.prepare(PHOTO, llm, "GigaChat-2-Max", stages.append)
    assert ready.built_by == "GigaChat-2-Max" and not ready.note and stages == ["map"]
    assert ready.lesson.misconceptions and ready.lesson.cases
    check_lesson(ready.lesson)
    stages.clear()
    topic = materials.prepare("производная", llm, "GigaChat-2-Max", stages.append)
    assert topic.generated and stages == ["write", "map"] and "Производная" in topic.lesson.source_text


def test_offline_dialogue_over_built_lesson_passes_rules():
    lesson = materials.build(PHOTO)
    tutor = Tutor(lesson, None)
    tutor.start()
    first = lesson.concept(lesson.curriculum[0])
    for line in [first.expectation, "не знаю", "просто скажи ответ", "потому что без света растение не получит энергию, например в темноте", "не помню"]:
        tr = tutor.step(line)
        assert not verifier.rule_check(tr.reply, Plan(tr.phase, tr.target, tr.move, "")), tr.reply
    tr = tutor.jump("apply")
    assert tr.move == "apply_case" and tr.reply.endswith("?")
    tr = tutor.step("Я бы поставил рассаду ближе к окну, потому что скорость фотосинтеза зависит от освещённости и растению нужно больше света для роста.")
    assert tr.phase in ("apply", "reflect")
