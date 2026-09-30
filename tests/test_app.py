from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "app.py")


def event(at: AppTest, n: int, **ev) -> AppTest:
    """Событие из интерфейса: компонент возвращает его как значение виджета «ui»."""
    at.session_state["ui"] = {"id": n, **ev}
    at.run()
    assert not at.exception, at.exception
    return at


def test_app_runs_chats_and_dialogue_without_errors(monkeypatch):
    # Приложение целиком (без браузера и без GigaChat): главная, пример, свой текст, тема, ответ ученика, удаление разбора.
    monkeypatch.delenv("GIGACHAT_CREDENTIALS", raising=False)
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    event(at, 1, type="hello", client="test-client")
    assert at.session_state["client"] == "test-client" and at.session_state["active"] is None

    event(at, 2, type="example", name="okr")
    okr = at.session_state["active"]
    assert okr

    event(at, 3, type="new", text=(ROOT / "lessons" / "examples" / "fotosintez.md").read_text())
    photo = at.session_state["active"]
    assert photo and photo != okr and at.session_state["rejected"] is None

    event(at, 4, type="send", text="Фотосинтез — это когда растения превращают энергию света в энергию химических связей.")
    event(at, 5, type="new", text="фотосинтез")  # тема без GigaChat: разбор не начинается, текст возвращается в поле
    assert at.session_state["rejected"]["text"] == "фотосинтез" and at.session_state["active"] == photo

    event(at, 6, type="open", chat=okr)
    assert at.session_state["active"] == okr
    event(at, 7, type="delete", chat=okr)
    assert at.session_state["active"] is None
