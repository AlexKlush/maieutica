import base64

import httpx
import pytest

import maieutica.llm as llm
from maieutica import load_lesson
from maieutica.analyzer import Analysis
from maieutica.engine import Tutor
from maieutica.llm import APIError, GigaChat, LLMUnavailable, find_credentials, key_problem, normalize_key

from .fake_gigachat import FakeGigaChat

KEY = base64.b64encode(b"0198c1a2-7a1e-7c3a-9f00-000000000001:0198c1a2-7a1e-7c3a-9f00-000000000002").decode()
MSG = [{"role": "user", "content": "привет"}]


def client(fake, **kw) -> GigaChat:
    return GigaChat(credentials=KEY, transport=httpx.MockTransport(fake), **kw)


# --- ключ ---------------------------------------------------------------------------------------------------------

def test_normalize_key_strips_what_people_paste():
    assert normalize_key(f' "Basic {KEY[:20]}\n{KEY[20:]}" ') == KEY


def test_key_problem():
    assert key_problem(KEY) == ""
    assert "Authorization key" in key_problem("0198c1a2-7a1e-7c3a-9f00-000000000001")  # Client ID вместо ключа
    assert "Authorization key" in key_problem("не ключ")


@pytest.fixture
def no_env(monkeypatch, tmp_path):
    for name in ("GIGACHAT_CREDENTIALS", "GIGACHAT_SCOPE", "GIGACHAT_CLIENT_ID", "GIGACHAT_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "ROOT", tmp_path)
    monkeypatch.setattr(llm, "_secrets", lambda: {})
    return tmp_path


def test_credentials_from_dotenv(no_env):
    (no_env / ".env").write_text(f'﻿# ключ GigaChat\nexport GIGACHAT_CREDENTIALS="{KEY}"\nGIGACHAT_SCOPE=gigachat_api_b2b\n', encoding="utf-8")
    found = find_credentials()
    assert (found.key, found.scope, found.source) == (KEY, "GIGACHAT_API_B2B", "файл .env")


def test_credentials_from_client_id_and_secret(no_env, monkeypatch):
    monkeypatch.setenv("GIGACHAT_CLIENT_ID", "id")
    monkeypatch.setenv("GIGACHAT_CLIENT_SECRET", "secret")
    assert base64.b64decode(find_credentials().key) == b"id:secret"


def test_no_credentials(no_env):
    assert find_credentials() is None
    with pytest.raises(LLMUnavailable):
        GigaChat()


# --- авторизация --------------------------------------------------------------------------------------------------

def test_scope_is_detected_when_not_set():
    fake = FakeGigaChat(scope="GIGACHAT_API_B2B")
    g = client(fake)
    ok, msg = g.ping()
    assert ok and g.scope == "GIGACHAT_API_B2B" and "подобран GIGACHAT_API_B2B" in msg


def test_explicit_scope_is_not_guessed():
    g = client(FakeGigaChat(scope="GIGACHAT_API_B2B"), scope="GIGACHAT_API_PERS")
    ok, msg = g.ping()
    assert not ok and "OAuth 400" in msg and "уберите явный scope" in msg


def test_wrong_key_explains_what_is_wrong():
    g = GigaChat(credentials="0198c1a2-7a1e-7c3a-9f00-000000000001", transport=httpx.MockTransport(FakeGigaChat(fail_auth=True)))
    ok, msg = g.ping()
    assert not ok and "OAuth 401" in msg and "Client ID" in msg


def test_rejected_token_is_not_retried_forever():
    fake = FakeGigaChat()
    g = client(lambda r: httpx.Response(401, json={"message": "Unauthorized"}) if r.url.path.endswith("/models") else fake(r))
    ok, msg = g.ping()
    assert not ok and "401" in msg
    assert sum(1 for c in fake.calls if c["path"].endswith("/oauth")) == 2  # первый токен + одна повторная авторизация


# --- адрес API и таймауты -------------------------------------------------------------------------------------------

def test_falls_back_to_second_api_host():
    fake, hosts = FakeGigaChat(), []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "gigachat.devices.sberbank.ru":
            raise httpx.ConnectError("blocked", request=request)
        return fake(request)

    g = client(handler)
    assert g.ping()[0] and g.api_url == "https://api.giga.chat/v1"
    g.chat(MSG, model="GigaChat-2-Max")
    assert hosts.count("gigachat.devices.sberbank.ru") == 1  # дальше работает со вторым адресом


def test_read_timeout_is_retried_once(monkeypatch):
    slept, attempts, fake = [], [], FakeGigaChat()
    monkeypatch.setattr(llm.time, "sleep", slept.append)

    def handler(request):
        if request.url.path.endswith("/chat/completions"):
            attempts.append(1)
            raise httpx.ReadTimeout("slow", request=request)
        return fake(request)

    with pytest.raises(LLMUnavailable, match="не ответил вовремя"):
        client(handler).chat(MSG, model="GigaChat-2-Max")
    assert len(attempts) == 2 and not slept


# --- модели ---------------------------------------------------------------------------------------------------------

def test_exhausted_model_is_replaced_in_app_mode():
    fake = FakeGigaChat(exhausted={"GigaChat-2-Max"})
    g = client(fake, substitute_models=True)
    r = g.chat(MSG, model="GigaChat-2-Max")
    assert r.model.startswith("GigaChat-2-Pro") and r.requested == "GigaChat-2-Max"
    g.chat(MSG, model="GigaChat-2-Max")
    asked = [c["body"]["model"] for c in fake.calls if c["path"].endswith("/chat/completions")]
    assert asked == ["GigaChat-2-Max", "GigaChat-2-Pro", "GigaChat-2-Pro"]  # недоступную модель больше не спрашиваем


def test_missing_models_fall_through_the_chain():
    g = client(FakeGigaChat(missing={"GigaChat-3-Ultra", "GigaChat-2-Max", "GigaChat-2-Pro"}), substitute_models=True)
    r = g.chat(MSG, model="GigaChat-3-Ultra")
    assert r.model.startswith("GigaChat-2:") and r.requested == "GigaChat-3-Ultra"


def test_eval_mode_never_substitutes_models():
    with pytest.raises(APIError) as e:
        client(FakeGigaChat(exhausted={"GigaChat-2-Max"})).chat(MSG, model="GigaChat-2-Max")
    assert e.value.status == 402


def test_all_models_unavailable():
    g = client(FakeGigaChat(exhausted={"GigaChat-2-Max", "GigaChat-2-Pro", "GigaChat-2"}), substitute_models=True)
    with pytest.raises(LLMUnavailable, match="ни одна модель"):
        g.chat(MSG, model="GigaChat-2-Max")
    ok, _ = g.ping()  # явная проверка начинает с чистого листа
    assert ok and not g.unavailable


def test_substitution_is_visible_in_the_trace():
    tutor = Tutor(load_lesson("okr"), client(FakeGigaChat(exhausted={"GigaChat-2-Max"}), substitute_models=True))
    tutor.start()
    tr = tutor.step("Честно, не знаю.")
    assert not tr.offline
    assert all(s.model.startswith("GigaChat-2-Pro") for s in tr.stages if s.key != "plan")
    assert any("замена: GigaChat-2-Max недоступна" in s.note for s in tr.stages)


def test_status_reflects_real_contact(monkeypatch):
    g = client(FakeGigaChat())
    assert g.status() == ("unknown", "")
    g.chat(MSG, model="GigaChat-2-Max")
    assert g.status() == ("ok", "")
    g._down("обрыв")
    assert g.status() == ("down", "обрыв")
    monkeypatch.setattr(llm.time, "time", lambda: g._down_until + 1)
    assert g.status() == ("unknown", "")  # пауза кончилась, но связь не проверена


# --- разбор ответов модели ------------------------------------------------------------------------------------------

def test_malformed_function_arguments_do_not_crash():
    lesson = load_lesson("okr")
    a = Analysis.from_args(
        {"intent": ["answer"], "verdict": "correct", "covered_points": "1, 3", "evidence": None, "misconceptions": None,
         "resolved": [{"x": 1}, "numbers_in_objective"], "depth": "два", "affect": None, "question_reveals_target": "false"},
        lesson, 3,
    )
    assert (a.intent, a.covered_points, a.evidence, a.misconceptions, a.resolved, a.depth, a.question_reveals_target) == (
        "answer", [1, 3], [], [], ["numbers_in_objective"], 1, False)


def test_auditor_string_false_is_not_a_rejection():
    fake = FakeGigaChat()

    def handler(request):
        resp = fake(request)
        if request.url.path.endswith("/chat/completions") and b"audit_tutor_reply" in request.content:
            data = resp.json()
            data["choices"][0]["message"]["function_call"]["arguments"] = {k: "false" for k in ("reveals_answer", "false_praise", "invented_facts", "ignores_move")} | {"comment": ""}
            return httpx.Response(200, json=data)
        return resp

    tutor = Tutor(load_lesson("okr"), client(handler))
    tutor.start()
    tr = tutor.step("Objective — это «увеличить выручку на 20%», чем конкретнее цифра, тем лучше.")
    assert tr.verifier["checked_by_llm"] and not tr.verifier["llm_issues"]
