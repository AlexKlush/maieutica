from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def test_app_runs_and_builds_state_without_errors():
    # Приложение запускается целиком (без браузера): собирает состояние для интерфейса и не падает.
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    tutor = at.session_state["tutor"]
    assert tutor.traces and tutor.traces[0].reply
