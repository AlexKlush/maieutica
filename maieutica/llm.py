from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
# Адрес API, на котором собирался конвейер, и новый официальный адрес Сбера (его по умолчанию берёт SDK gigachat).
# Если до первого не дойти, клиент пробует второй и дальше работает с тем, что ответил.
API_URLS = ("https://gigachat.devices.sberbank.ru/api/v1", "https://api.giga.chat/v1")
API_URL = API_URLS[0]
# Scope зависит от типа ключа: физлицо, ИП/юрлицо по предоплате, юрлицо по постоплате. Если scope не задан явно, подбирается сам.
SCOPES = ("GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP")
# Замены для приложения, если модель недоступна: 402 — закончились токены этой модели, 403 — нет доступа к ней, 404 — такой модели нет.
FALLBACK_MODELS = ("GigaChat-2-Max", "GigaChat-2-Pro", "GigaChat-2")
CA_BUNDLE = ROOT / "certs" / "russian_trusted_ca_bundle.pem"
# После сбоя соединения GigaChat считается недоступным: тьютор отвечает в автономном режиме, а не ждёт таймаутов.
# Пауза удваивается при каждом следующем сбое подряд (45 с, 90 с, … до 10 минут) и сбрасывается после первого успешного ответа.
COOLDOWN = 45.0
COOLDOWN_MAX = 600.0
# Ошибки, при которых повторять запрос бессмысленно: до сервера не дойти (сеть закрыта, прокси отказал, TLS не сошёлся).
NO_CONNECTION = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, httpx.UnsupportedProtocol)
NO_KEY = "Не найден ключ GigaChat: задайте GIGACHAT_CREDENTIALS (переменная окружения, .streamlit/secrets.toml или файл .env) или вставьте ключ в боковой панели приложения"

# Freemium даёт один одновременный поток на ключ: все вызовы с одним ключом (из всех сессий) идут через общий замок.
_GATES: dict[str, threading.Lock] = {}
_GATES_LOCK = threading.Lock()


def _gate(key: str) -> threading.Lock:
    digest = hashlib.sha256(key.encode()).hexdigest()
    with _GATES_LOCK:
        return _GATES.setdefault(digest, threading.Lock())


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """Нет ключа, нет сети или ключ не принят: пока это не изменится, GigaChat недоступен."""


class APIError(LLMError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    cached: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion


@dataclass
class LLMResult:
    content: str
    args: dict | None
    usage: Usage
    latency: float
    model: str
    queued: float = 0.0
    raw: dict = field(default_factory=dict)
    requested: str = ""  # если ответила замена: какую модель просили


# --- ключ -------------------------------------------------------------------------------------------------------

@dataclass
class Credentials:
    key: str
    scope: str = ""  # пусто — подобрать автоматически
    source: str = ""


def normalize_key(raw: Any) -> str:
    """Ключ часто копируют с кавычками, префиксом «Basic» или переносами строк — убираем это."""
    key = str(raw or "").strip().strip("\"'").strip()
    if key[:6].lower() == "basic ":
        key = key[6:]
    return "".join(key.split())


def key_problem(key: str) -> str:
    """Пусто, если строка похожа на Authorization key (base64 от «Client ID:Client Secret»), иначе — подсказка, что не так."""
    try:
        text = base64.b64decode(key + "=" * (-len(key) % 4), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        text = ""
    if ":" in text:
        return ""
    return ("строка не похожа на Authorization key. Нужен ключ авторизации из личного кабинета GigaChat API "
            "(«Настройки API» → «Получить ключ») — длинная строка base64, а не Client ID или Client Secret по отдельности")


def _dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        out[name] = value.strip().strip("\"'")
    return out


def _secrets() -> dict[str, str]:
    try:
        import streamlit as st

        return {k: str(v) for k, v in st.secrets.items() if not isinstance(v, dict)}
    except Exception:
        return {}


def find_credentials() -> Credentials | None:
    """Ключ из первого источника, где он есть: переменные окружения, Streamlit Secrets, файл .env в корне проекта."""
    sources: list[tuple[str, Callable[[], dict[str, str]]]] = [
        ("переменная окружения", lambda: dict(os.environ)),
        ("Streamlit Secrets", _secrets),
        ("файл .env", lambda: _dotenv(ROOT / ".env")),
    ]
    for source, load in sources:
        values = load()
        key = normalize_key(values.get("GIGACHAT_CREDENTIALS"))
        if not key:
            cid, secret = values.get("GIGACHAT_CLIENT_ID", "").strip(), values.get("GIGACHAT_CLIENT_SECRET", "").strip()
            if cid and secret:
                key = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        if key:
            scope = values.get("GIGACHAT_SCOPE") or os.environ.get("GIGACHAT_SCOPE", "")
            return Credentials(key, scope.strip().upper(), source)
    return None


def load_credentials() -> tuple[str, str]:
    found = find_credentials()
    if found is None:
        raise LLMUnavailable(NO_KEY)
    return found.key, found.scope or SCOPES[0]


# --- соединение -------------------------------------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    # Системное хранилище (в нём и сертификат корпоративного прокси, если он есть), корни certifi (у Python с python.org
    # на macOS своих корней нет) и сертификаты Минцифры, которыми подписаны серверы GigaChat. Проверка сертификата
    # и имени хоста остаётся полной.
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    if CA_BUNDLE.exists():
        ctx.load_verify_locations(cafile=str(CA_BUNDLE))
    return ctx


def _why(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:200]


def _host(url: str) -> str:
    return urlparse(url).netloc


def _body(r: httpx.Response) -> str:
    return " ".join(r.text.split())[:200]


class GigaChat:
    name = "GigaChat"

    def __init__(
        self,
        credentials: str | None = None,
        scope: str | None = None,
        timeout: float = 90.0,
        auth_url: str | None = None,
        api_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        fail_fast: bool = False,
        substitute_models: bool = False,
        source: str = "",
    ):
        if credentials is None:
            found = find_credentials()
            if found is None:
                raise LLMUnavailable(NO_KEY)
            credentials, scope, source = found.key, scope or found.scope, found.source
        self.credentials = normalize_key(credentials)
        self.source = source
        # Scope, заданный явно (аргумент, GIGACHAT_SCOPE), не подбираем: ошибка в нём должна быть видна.
        self.scope_fixed = bool((scope or "").strip())
        self.scope = (scope or "").strip().upper() or SCOPES[0]
        self.scope_note = ""
        # fail_fast: пока GigaChat помечен недоступным, chat() сразу бросает LLMUnavailable, а не ходит в сеть по очереди.
        # substitute_models: при 402/403/404 брать следующую модель из FALLBACK_MODELS. Оба режима — для приложения;
        # оценка качества (eval) оставляет их выключенными, чтобы сбои и подмены моделей не искажали результаты.
        self.fail_fast = fail_fast
        self.substitute_models = substitute_models
        # Адреса можно переопределить (стенд, локальная заглушка в тестах): GIGACHAT_AUTH_URL / GIGACHAT_API_URL (или GIGACHAT_BASE_URL, как в SDK).
        self.auth_url = auth_url or os.environ.get("GIGACHAT_AUTH_URL") or AUTH_URL
        base = api_url or os.environ.get("GIGACHAT_API_URL") or os.environ.get("GIGACHAT_BASE_URL")
        self.api_urls = [base.rstrip("/")] if base else list(API_URLS)
        self.api_url = self.api_urls[0]
        self.http = httpx.Client(verify=_ssl_context(), timeout=httpx.Timeout(timeout, connect=10.0), transport=transport)
        self._gate = _gate(self.credentials)
        self._token = ""
        self._expires = 0.0
        self._token_lock = threading.Lock()
        self._down_until = 0.0
        self._fails = 0
        self.last_error = ""
        self.ok_at = 0.0
        self.unavailable: dict[str, str] = {}  # модель → почему её нельзя использовать (только при substitute_models)
        self.model_ids: list[str] = []  # модели, которые API вернул при последней проверке

    # --- состояние ---

    def available(self) -> bool:
        """False, пока действует пауза после сбоя соединения: за неё тьютор работает без нейросети."""
        return time.time() >= self._down_until

    def status(self) -> tuple[str, str]:
        """«ok» — GigaChat недавно отвечал, «down» — пауза после сбоя (с причиной), «unknown» — ключ есть, но связь ещё не проверялась."""
        if not self.available():
            return "down", self.last_error
        return ("ok" if self.ok_at else "unknown"), ""

    def retry_in(self) -> int:
        return max(0, round(self._down_until - time.time()))

    def reset(self) -> None:
        self._down_until = 0.0
        self._fails = 0
        self.last_error = ""

    def _down(self, reason: str) -> LLMUnavailable:
        self.ok_at = 0.0  # после паузы связь снова неизвестна, а не «подключено»
        self._fails += 1
        self._down_until = time.time() + min(COOLDOWN * 2 ** (self._fails - 1), COOLDOWN_MAX)
        self.last_error = reason
        return LLMUnavailable(reason)

    # --- авторизация ---

    def _auth(self, force: bool = False) -> str:
        with self._token_lock:
            if not force and self._token and time.time() < self._expires - 60:
                return self._token
            scopes = [self.scope] + ([] if self.scope_fixed else [s for s in SCOPES if s != self.scope])
            first = ""
            for scope in scopes:
                try:
                    r = self.http.post(
                        self.auth_url,
                        headers={
                            "Authorization": f"Basic {self.credentials}",
                            "RqUID": str(uuid.uuid4()),
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Accept": "application/json",
                        },
                        data={"scope": scope},
                    )
                except httpx.HTTPError as e:
                    raise self._down(f"нет соединения с сервером авторизации {_host(self.auth_url)} ({_why(e)})") from e
                if r.status_code == 200:
                    try:
                        data = r.json()
                        self._token = data["access_token"]
                    except (ValueError, KeyError, TypeError) as e:
                        raise self._down(f"OAuth: неожиданный ответ сервера авторизации ({_body(r)})") from e
                    self._expires = (data.get("expires_at") or 0) / 1000 or time.time() + 1500
                    if scope != self.scope:
                        self.scope_note = f"scope {self.scope} не подошёл к ключу, подобран {scope}"
                        self.scope = scope
                    return self._token
                first = first or f"OAuth {r.status_code}: {_body(r)}"
                if r.status_code not in (400, 401, 403):
                    break  # 429 или сбой сервера — дело не в scope, перебирать дальше бессмысленно
            if first.startswith(("OAuth 400", "OAuth 401", "OAuth 403")):
                hint = key_problem(self.credentials)
                first += f". Ключ не принят (пробовал scope: {', '.join(scopes)})"
                if hint:
                    first += f": {hint}"
                elif self.scope_fixed:
                    first += " — проверьте ключ или уберите явный scope (GIGACHAT_SCOPE), чтобы он подобрался сам"
                else:
                    first += " — проверьте, что это действующий Authorization key"
            raise self._down(first)

    # --- запросы ---

    def _request(self, method: str, path: str, *, json_body: dict | None = None, session_id: str | None = None) -> dict:
        delay = 1.5
        last = ""
        timeouts = unauthorized = 0
        tried = {self.api_url}
        for _ in range(8):
            try:
                headers = {"Authorization": f"Bearer {self._auth(force=last == '401')}", "Accept": "application/json"}
                if session_id:
                    headers["X-Session-ID"] = session_id
                r = self.http.request(method, self.api_url + path, headers=headers, json=json_body)
            except LLMError:
                raise
            except NO_CONNECTION as e:
                rest = [u for u in self.api_urls if u not in tried]
                if rest:  # этот адрес API недоступен — пробуем следующий
                    self.api_url = rest[0]
                    tried.add(self.api_url)
                    last = type(e).__name__
                    continue
                raise self._down(f"нет соединения с GigaChat ({', '.join(sorted(_host(u) for u in tried))}: {_why(e)})") from e
            except httpx.TimeoutException as e:
                timeouts += 1
                if timeouts >= 2:
                    raise self._down(f"GigaChat не ответил вовремя ({_why(e)})") from e
                last = type(e).__name__
                continue
            except httpx.HTTPError as e:
                last = type(e).__name__
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError as e:
                    raise self._down(f"GigaChat вернул не JSON ({_body(r)})") from e
                self.reset()
                self.ok_at = time.time()
                return data
            last = str(r.status_code)
            if r.status_code == 401:
                unauthorized += 1
                if unauthorized >= 2:
                    raise self._down(f"GigaChat 401: токен не принят API ({_body(r)})")
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            raise APIError(r.status_code, f"GigaChat {r.status_code}: {_body(r)}")
        raise self._down(f"GigaChat недоступен после повторов (последний ответ: {last})")

    def models(self) -> list[str]:
        with self._gate:
            data = self._request("GET", "/models")
        return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]

    def ping(self) -> tuple[bool, str]:
        """Явная проверка связи без расхода токенов: OAuth + список моделей. Сбрасывает паузу и подмены моделей, не бросает исключений."""
        self.reset()
        self.unavailable.clear()
        t0 = time.perf_counter()
        try:
            ids = self.models()
        except LLMError as e:
            return False, str(e)
        self.model_ids = ids
        note = f"; {self.scope_note}" if self.scope_note else ""
        return True, f"GigaChat отвечает: {_host(self.api_url)}, {time.perf_counter() - t0:.1f} с, моделей {len(ids)}, scope {self.scope}{note}"

    def resolve(self, model: str) -> str:
        """Модель, которую реально спросить: запрошенная или, если она недоступна, следующая из FALLBACK_MODELS."""
        if model not in self.unavailable:
            return model
        chain = list(FALLBACK_MODELS)
        start = chain.index(model) + 1 if model in chain else 0
        for m in chain[start:]:
            if m not in self.unavailable:
                return m
        reasons = "; ".join(f"{m}: {why}" for m, why in self.unavailable.items())
        raise self._down(f"ни одна модель GigaChat недоступна ({reasons})")

    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float = 0.7,
        top_p: float | None = None,
        max_tokens: int = 800,
        function: dict | None = None,
        session_id: str | None = None,
        repetition_penalty: float | None = None,
    ) -> LLMResult:
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if top_p is not None:
            body["top_p"] = top_p
        if repetition_penalty is not None:
            body["repetition_penalty"] = repetition_penalty
        if function:
            body["functions"] = [function]
            body["function_call"] = {"name": function["name"]}
        t_wait = time.perf_counter()
        with self._gate:
            if self.fail_fast and not self.available():
                raise LLMUnavailable(self.last_error or "GigaChat временно недоступен")
            queued = time.perf_counter() - t_wait
            t0 = time.perf_counter()
            while True:
                body["model"] = self.resolve(model) if self.substitute_models else model
                try:
                    data = self._request("POST", "/chat/completions", json_body=body, session_id=session_id)
                    break
                except APIError as e:
                    if not self.substitute_models or e.status not in (402, 403, 404):
                        raise
                    self.unavailable[body["model"]] = {402: "закончились токены", 403: "нет доступа"}.get(e.status, "модель не найдена")
            latency = time.perf_counter() - t0
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Неожиданный ответ GigaChat: {str(data)[:200]}") from e
        u = data.get("usage") or {}
        usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("precached_prompt_tokens", 0))
        args = None
        if function:
            fc = choice.get("function_call") or {}
            args = fc.get("arguments")
            if isinstance(args, str):
                args = _parse_json(args)
            if not isinstance(args, dict):
                args = _parse_json(choice.get("content") or "")
            if args is None:
                raise LLMError("Модель не вернула структурированный ответ")
        return LLMResult(
            content=(choice.get("content") or "").strip(),
            args=args,
            usage=usage,
            latency=latency,
            model=data.get("model", body["model"]),
            queued=queued,
            raw=data,
            requested=model if body["model"] != model else "",
        )


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


def as_bool(x: Any) -> bool:
    """Модель иногда отвечает в функции строкой «false» вместо false — bool("false") был бы True."""
    if isinstance(x, str):
        return x.strip().lower() in ("true", "1", "yes", "да")
    return bool(x)
