from __future__ import annotations

import json
import os
import re
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

# Freemium даёт один одновременный поток на весь ключ: все вызовы из всех сессий идут через этот замок.
_GATE = threading.Lock()


class LLMError(RuntimeError):
    pass


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
        raise LLMError("Не найден ключ GIGACHAT_CREDENTIALS (env, st.secrets или .env)")
    return creds, scope


class GigaChat:
    def __init__(self, credentials: str | None = None, scope: str | None = None, timeout: float = 90.0):
        if credentials is None:
            credentials, scope = load_credentials()
        self.credentials = credentials
        self.scope = scope or "GIGACHAT_API_PERS"
        verify: Any = str(CA_BUNDLE) if CA_BUNDLE.exists() else True
        self.http = httpx.Client(verify=verify, timeout=timeout)
        self._token = ""
        self._expires = 0.0
        self._token_lock = threading.Lock()

    def _auth(self, force: bool = False) -> str:
        with self._token_lock:
            if not force and self._token and time.time() < self._expires - 60:
                return self._token
            r = self.http.post(
                AUTH_URL,
                headers={
                    "Authorization": f"Basic {self.credentials}",
                    "RqUID": str(uuid.uuid4()),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={"scope": self.scope},
            )
            if r.status_code != 200:
                raise LLMError(f"OAuth {r.status_code}: {r.text[:200]}")
            data = r.json()
            self._token = data["access_token"]
            self._expires = data.get("expires_at", 0) / 1000 or time.time() + 1500
            return self._token

    def _request(self, method: str, path: str, *, json_body: dict | None = None, session_id: str | None = None) -> dict:
        delay = 1.5
        last = ""
        for attempt in range(6):
            headers = {"Authorization": f"Bearer {self._auth(force=attempt > 0 and last == '401')}", "Accept": "application/json"}
            if session_id:
                headers["X-Session-ID"] = session_id
            try:
                r = self.http.request(method, API_URL + path, headers=headers, json=json_body)
            except httpx.HTTPError as e:
                last = type(e).__name__
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 200:
                return r.json()
            last = str(r.status_code)
            if r.status_code in (401, 429) or r.status_code >= 500:
                time.sleep(delay if r.status_code != 401 else 0)
                delay = min(delay * 2, 20)
                continue
            raise LLMError(f"GigaChat {r.status_code}: {r.text[:300]}")
        raise LLMError(f"GigaChat недоступен после повторов (последний ответ: {last})")

    def models(self) -> list[str]:
        with _GATE:
            data = self._request("GET", "/models")
        return [m["id"] for m in data.get("data", [])]

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
