from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1"
CA_BUNDLE = Path(__file__).resolve().parent.parent / "certs" / "russian_trusted_ca_bundle.pem"
# После сбоя соединения GigaChat считается недоступным: тьютор отвечает в автономном режиме, а не ждёт таймаутов.
# Пауза удваивается при каждом следующем сбое подряд (45 с, 90 с, … до 10 минут) и сбрасывается после первого успешного ответа.
COOLDOWN = 45.0
COOLDOWN_MAX = 600.0
# Ошибки, при которых повторять запрос бессмысленно: до сервера не дойти (сеть закрыта, прокси отказал, TLS не сошёлся).
NO_CONNECTION = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, httpx.UnsupportedProtocol)

# Freemium даёт один одновременный поток на весь ключ: все вызовы из всех сессий идут через этот замок.
_GATE = threading.Lock()


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """Нет ключа, нет сети или ключ не принят: пока это не изменится, GigaChat недоступен."""


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


def load_credentials() -> tuple[str, str]:
    creds = os.environ.get("GIGACHAT_CREDENTIALS", "").strip()
    scope = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
    if not creds:
        try:
            import streamlit as st

            creds = str(st.secrets.get("GIGACHAT_CREDENTIALS", "")).strip()
            scope = str(st.secrets.get("GIGACHAT_SCOPE", scope)).strip()
        except Exception:
            pass
    if not creds:
        env = Path(__file__).resolve().parent.parent / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("GIGACHAT_CREDENTIALS="):
                    creds = line.split("=", 1)[1].strip()
                elif line.startswith("GIGACHAT_SCOPE="):
                    scope = line.split("=", 1)[1].strip() or scope
    if not creds:
        raise LLMUnavailable("Не найден ключ GIGACHAT_CREDENTIALS (env, st.secrets или .env)")
    return creds, scope


def _ssl_context() -> ssl.SSLContext:
    # Системные корни (нужны, если трафик идёт через корпоративный прокси) плюс сертификаты Минцифры, которыми подписан GigaChat.
    ctx = ssl.create_default_context()
    if CA_BUNDLE.exists():
        ctx.load_verify_locations(cafile=str(CA_BUNDLE))
    return ctx


def _why(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:200]


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
    ):
        if credentials is None:
            credentials, scope = load_credentials()
        self.credentials = credentials
        self.scope = scope or "GIGACHAT_API_PERS"
        # fail_fast: пока GigaChat помечен недоступным, chat() сразу бросает LLMUnavailable, а не ходит в сеть по очереди
        # (для приложения; оценка качества оставляет False и повторяет запросы как раньше).
        self.fail_fast = fail_fast
        # Адреса можно переопределить (стенд, локальная заглушка в тестах): GIGACHAT_AUTH_URL / GIGACHAT_API_URL.
        self.auth_url = auth_url or os.environ.get("GIGACHAT_AUTH_URL") or AUTH_URL
        self.api_url = (api_url or os.environ.get("GIGACHAT_API_URL") or API_URL).rstrip("/")
        self.http = httpx.Client(verify=_ssl_context(), timeout=httpx.Timeout(timeout, connect=10.0), transport=transport)
        self._token = ""
        self._expires = 0.0
        self._token_lock = threading.Lock()
        self._down_until = 0.0
        self._fails = 0
        self.last_error = ""

    def available(self) -> bool:
        """False, пока действует пауза после сбоя соединения: за неё тьютор работает без нейросети."""
        return time.time() >= self._down_until

    def reset(self) -> None:
        self._down_until = 0.0
        self._fails = 0
        self.last_error = ""

    def _down(self, reason: str) -> LLMUnavailable:
        self._fails += 1
        self._down_until = time.time() + min(COOLDOWN * 2 ** (self._fails - 1), COOLDOWN_MAX)
        self.last_error = reason
        return LLMUnavailable(reason)

    def _auth(self, force: bool = False) -> str:
        with self._token_lock:
            if not force and self._token and time.time() < self._expires - 60:
                return self._token
            try:
                r = self.http.post(
                    self.auth_url,
                    headers={
                        "Authorization": f"Basic {self.credentials}",
                        "RqUID": str(uuid.uuid4()),
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                    data={"scope": self.scope},
                )
            except httpx.HTTPError as e:
                raise self._down(f"OAuth: нет соединения ({_why(e)})") from e
            if r.status_code != 200:
                raise self._down(f"OAuth {r.status_code}: {r.text[:200]}")
            data = r.json()
            self._token = data["access_token"]
            self._expires = data.get("expires_at", 0) / 1000 or time.time() + 1500
            return self._token

    def _request(self, method: str, path: str, *, json_body: dict | None = None, session_id: str | None = None) -> dict:
        delay = 1.5
        last = ""
        for attempt in range(6):
            try:
                headers = {"Authorization": f"Bearer {self._auth(force=attempt > 0 and last == '401')}", "Accept": "application/json"}
                if session_id:
                    headers["X-Session-ID"] = session_id
                r = self.http.request(method, self.api_url + path, headers=headers, json=json_body)
            except LLMError:
                raise
            except NO_CONNECTION as e:
                raise self._down(f"нет соединения с GigaChat ({_why(e)})") from e
            except httpx.HTTPError as e:
                last = type(e).__name__
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 200:
                self.reset()
                return r.json()
            last = str(r.status_code)
            if r.status_code in (401, 429) or r.status_code >= 500:
                time.sleep(delay if r.status_code != 401 else 0)
                delay = min(delay * 2, 20)
                continue
            raise LLMError(f"GigaChat {r.status_code}: {r.text[:300]}")
        raise self._down(f"GigaChat недоступен после повторов (последний ответ: {last})")

    def models(self) -> list[str]:
        with _GATE:
            data = self._request("GET", "/models")
        return [m["id"] for m in data.get("data", [])]

    def ping(self) -> tuple[bool, str]:
        """Проверка связи без расхода токенов: OAuth + список моделей. Не бросает исключений."""
        t0 = time.perf_counter()
        try:
            ids = self.models()
        except LLMError as e:
            return False, str(e)
        return True, f"GigaChat отвечает ({time.perf_counter() - t0:.1f} с, моделей: {len(ids)})"

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
        with _GATE:
            if self.fail_fast and not self.available():
                raise LLMUnavailable(self.last_error or "GigaChat временно недоступен")
            queued = time.perf_counter() - t_wait
            t0 = time.perf_counter()
            data = self._request("POST", "/chat/completions", json_body=body, session_id=session_id)
            latency = time.perf_counter() - t0
        choice = data["choices"][0]["message"]
        u = data.get("usage", {})
        usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("precached_prompt_tokens", 0))
        args = None
        if function:
            fc = choice.get("function_call") or {}
            args = fc.get("arguments")
            if isinstance(args, str):
                args = _parse_json(args)
            if args is None:
                args = _parse_json(choice.get("content") or "")
            if args is None:
                raise LLMError("Модель не вернула структурированный ответ")
        return LLMResult(
            content=(choice.get("content") or "").strip(),
            args=args,
            usage=usage,
            latency=latency,
            model=data.get("model", model),
            queued=queued,
            raw=data,
        )


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
