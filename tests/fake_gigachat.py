"""Заглушка GigaChat API: OAuth, /models и /chat/completions (в том числе function_call).

Нужна, чтобы проверять весь путь запросов (OAuth → токен → chat/completions → разбор ответа) без настоящего ключа и без выхода в сеть.
Умеет изображать типичные сбои: ключ другого типа (нужен другой scope), закончились токены модели (402), модели нет (404).
Отвечает не умно: диагностику берёт у автономного режима, реплики — шаблонные. Для ручной проверки:

    python -m tests.fake_gigachat 8765 [--scope GIGACHAT_API_B2B] [--exhausted GigaChat-2-Max] [--missing GigaChat-2-Max]
    GIGACHAT_CREDENTIALS=ZmFrZTpmYWtl GIGACHAT_AUTH_URL=http://127.0.0.1:8765/api/v2/oauth GIGACHAT_API_URL=http://127.0.0.1:8765/api/v1 streamlit run app.py
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import httpx

from maieutica import load_lesson, offline

ROLES = {"system", "user", "assistant", "function"}
MODELS = ("GigaChat-2", "GigaChat-2-Pro", "GigaChat-2-Max", "GigaChat-3-Ultra")
REPLY = "Интересная мысль. А как бы вы объяснили это человеку, который впервые слышит про OKR?"


class FakeGigaChat:
    def __init__(self, token: str = "fake-token", fail_auth: bool = False, scope: str = "GIGACHAT_API_PERS", exhausted=(), missing=(), delay: float = 0.0):
        self.token = token
        self.fail_auth = fail_auth
        self.scope = scope  # единственный scope, который подходит к «ключу»
        self.exhausted = set(exhausted)
        self.missing = set(missing)
        self.delay = delay  # имитация задержки настоящего API (для ручной проверки анимаций)
        self.calls: list[dict] = []
        self.lesson = load_lesson("okr")

    def respond(self, method: str, path: str, headers: dict, raw: bytes) -> tuple[int, dict]:
        is_json = headers.get("content-type", "").startswith("application/json")
        body = json.loads(raw) if raw and is_json else None
        form = {k: v[0] for k, v in parse_qs(raw.decode()).items()} if raw and not is_json else {}
        self.calls.append({"method": method, "path": path, "headers": headers, "body": body, "form": form})
        if path.endswith("/oauth"):
            if self.fail_auth or not headers.get("authorization", "").startswith("Basic ") or not headers.get("rquid"):
                return 401, {"code": 6, "message": "credentials doesn't match db data"}
            if form.get("scope") != self.scope:
                return 400, {"code": 7, "message": "scope from db not fully includes consumed scope"}
            return 200, {"access_token": self.token, "expires_at": int((time.time() + 1800) * 1000)}
        if headers.get("authorization") != f"Bearer {self.token}":
            return 401, {"status": 401, "message": "Unauthorized"}
        if path.endswith("/models"):
            return 200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "salutedevices"} for m in MODELS if m not in self.missing]}
        if path.endswith("/chat/completions"):
            return self._chat(body or {})
        return 404, {"status": 404, "message": "not found"}

    def _chat(self, body: dict) -> tuple[int, dict]:
        if self.delay:
            time.sleep(self.delay)
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs or any(m.get("role") not in ROLES or not isinstance(m.get("content"), str) for m in msgs):
            return 400, {"status": 400, "message": "bad messages"}
        model = body.get("model")
        if model in self.missing or model not in MODELS:
            return 404, {"status": 404, "message": "No such model"}
        if model in self.exhausted:
            return 402, {"status": 402, "message": "Payment Required"}
        message: dict = {"role": "assistant", "content": REPLY}
        fn = (body.get("function_call") or {}).get("name")
        if body.get("functions") is not None and fn not in {f["name"] for f in body["functions"]}:
            return 400, {"status": 400, "message": "function_call must name one of functions"}
        if fn == "assess_student_turn":
            user = msgs[-1]["content"]
            target = (re.search(r"ЦЕЛЕВОЕ ПОНЯТИЕ: \[(\w+)\]", user) or [None, self.lesson.curriculum[0]])[1]
            student = user.split("РЕПЛИКА УЧЕНИКА (оцени её)\n", 1)[-1]
            args = offline.analyze(self.lesson, target, "explore", student)[1].args
            message = {"role": "assistant", "content": "", "function_call": {"name": fn, "arguments": args}}
        elif fn == "audit_tutor_reply":
            args = {"reveals_answer": False, "false_praise": False, "invented_facts": False, "ignores_move": False, "comment": ""}
            message = {"role": "assistant", "content": "", "function_call": {"name": fn, "arguments": args}}
        used = sum(len(m["content"]) for m in msgs) // 3
        return 200, {
            "choices": [{"message": message, "index": 0, "finish_reason": "function_call" if fn else "stop"}],
            "created": int(time.time()),
            "model": f"{model}:fake",
            "object": "chat.completion",
            "usage": {"prompt_tokens": used, "completion_tokens": 20, "total_tokens": used + 20, "precached_prompt_tokens": 0},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        status, data = self.respond(request.method, request.url.path, {k.lower(): v for k, v in request.headers.items()}, request.content)
        return httpx.Response(status, json=data)


def serve(port: int, fake: FakeGigaChat) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _go(self) -> None:
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            status, data = fake.respond(self.command, self.path.split("?")[0], {k.lower(): v for k, v in self.headers.items()}, raw)
            out = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = _go

        def log_message(self, fmt, *args):
            model = (fake.calls[-1].get("body") or {}).get("model", "") if fake.calls else ""
            print(f"{self.command} {self.path} {model} -> {args[1] if len(args) > 1 else ''}".rstrip(), flush=True)

    print(f"Заглушка GigaChat слушает на 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("port", type=int, nargs="?", default=8765)
    ap.add_argument("--scope", default="GIGACHAT_API_PERS")
    ap.add_argument("--exhausted", default="", help="модели через запятую, у которых «закончились токены» (402)")
    ap.add_argument("--missing", default="", help="модели через запятую, которых «нет» (404)")
    ap.add_argument("--fail-auth", action="store_true")
    ap.add_argument("--delay", type=float, default=0.0, help="секунд на каждый chat/completions")
    a = ap.parse_args()
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
    serve(a.port, FakeGigaChat(scope=a.scope, exhausted=split(a.exhausted), missing=split(a.missing), fail_auth=a.fail_auth, delay=a.delay))
