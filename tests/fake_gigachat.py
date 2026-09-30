"""Заглушка GigaChat API: OAuth, /models и /chat/completions (в том числе function_call).

Нужна, чтобы проверять весь путь запросов (OAuth → токен → chat/completions → разбор ответа) без настоящего ключа и без выхода в сеть.
Отвечает не умно: диагностику берёт у автономного режима, реплики — шаблонные. Как заглушку для ручной проверки запускают так:

    python -m tests.fake_gigachat 8765
    GIGACHAT_CREDENTIALS=fake GIGACHAT_AUTH_URL=http://127.0.0.1:8765/oauth GIGACHAT_API_URL=http://127.0.0.1:8765/api/v1 streamlit run app.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from maieutica import load_lesson, offline

ROLES = {"system", "user", "assistant", "function"}
REPLY = "Интересная мысль. А как бы вы объяснили это человеку, который впервые слышит про OKR?"


class FakeGigaChat:
    def __init__(self, token: str = "fake-token", fail_auth: bool = False):
        self.token = token
        self.fail_auth = fail_auth
        self.calls: list[dict] = []
        self.lesson = load_lesson("okr")

    def respond(self, method: str, path: str, headers: dict, body: dict | None) -> tuple[int, dict]:
        self.calls.append({"method": method, "path": path, "headers": headers, "body": body})
        if path.endswith("/oauth"):
            if self.fail_auth or not headers.get("authorization", "").startswith("Basic ") or not headers.get("rquid"):
                return 401, {"message": "Unauthorized"}
            return 200, {"access_token": self.token, "expires_at": int((time.time() + 1800) * 1000)}
        if headers.get("authorization") != f"Bearer {self.token}":
            return 401, {"message": "invalid token"}
        if path.endswith("/models"):
            return 200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "salutedevices"} for m in ("GigaChat-2", "GigaChat-2-Pro", "GigaChat-2-Max")]}
        if path.endswith("/chat/completions"):
            return self._chat(body or {})
        return 404, {"message": "not found"}

    def _chat(self, body: dict) -> tuple[int, dict]:
        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs or any(m.get("role") not in ROLES or not isinstance(m.get("content"), str) for m in msgs):
            return 400, {"message": "bad messages"}
        message: dict = {"role": "assistant", "content": REPLY}
        fn = (body.get("function_call") or {}).get("name")
        if body.get("functions") is not None and fn not in {f["name"] for f in body["functions"]}:
            return 400, {"message": "function_call must name one of functions"}
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
            "model": f"{body.get('model', 'GigaChat')}:fake",
            "object": "chat.completion",
            "usage": {"prompt_tokens": used, "completion_tokens": 20, "total_tokens": used + 20, "precached_prompt_tokens": 0},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") and request.content else None
        status, data = self.respond(request.method, request.url.path, {k.lower(): v for k, v in request.headers.items()}, body)
        return httpx.Response(status, json=data)


def serve(port: int) -> None:
    fake = FakeGigaChat()

    class Handler(BaseHTTPRequestHandler):
        def _go(self) -> None:
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            is_json = self.headers.get("content-type", "").startswith("application/json")
            status, data = fake.respond(self.command, self.path.split("?")[0], {k.lower(): v for k, v in self.headers.items()}, json.loads(raw) if raw and is_json else None)
            out = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = _go

        def log_message(self, fmt, *args):
            print(f"{self.command} {self.path} -> fake GigaChat", flush=True)

    print(f"Заглушка GigaChat слушает на 127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
