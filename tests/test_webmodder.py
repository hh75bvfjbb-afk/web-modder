"""Offline tests: key detection and failover, format conversion, approvals, and a full agent run against a fake
OpenAI-compatible server (one key rate-limited, one out of credit, one that works)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WM_MODS", str(tmp_path / "mods"))
    tk = os.environ.get("WM_TEST_TOOLKIT")
    if tk:
        monkeypatch.setenv("WM_TOOLKIT", tk)
    return tmp_path


# ------------------------------------------------------------------ fake provider
class Fake:
    """Answers by the bearer key: 'rl' -> 429, 'broke' -> 429 insufficient_quota, 'good' -> scripted replies."""

    def __init__(self):
        self.calls = []
        self.script = []

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, headers=None):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._send(200, {"data": [{"id": "fake-model"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = self.headers.get("Authorization", "").removeprefix("Bearer ")
                fake.calls.append((key, body))
                if key == "rl":
                    return self._send(429, {"error": {"message": "Rate limit reached"}}, {"Retry-After": "60"})
                if key == "broke":
                    return self._send(429, {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota"}})
                reply = fake.script.pop(0) if fake.script else {"content": "Готово."}
                self._send(200, {"model": "fake-model", "choices": [{"message": reply,
                                 "finish_reason": "tool_calls" if reply.get("tool_calls") else "stop"}],
                                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"


@pytest.fixture
def fake():
    f = Fake()
    base = f.start()
    yield f, base
    f.httpd.shutdown()


# ------------------------------------------------------------------ keys
def test_detect():
    from webmodder.keys import detect

    assert detect("sk-ant-api03-xxxx") == ["anthropic"]
    assert detect("sk-or-v1-abc") == ["openrouter"]
    assert detect("AIzaSyD...") == ["gemini"]
    assert detect("gsk_123") == ["groq"]
    assert detect("sk-proj-abc") == ["openai"]
    assert detect("sk-abcdef") == ["openai", "deepseek"]
    assert detect("1234abcd-12ab-34cd-56ef-1234567890ab:0123456789abcdef0123") == ["fal"]


def test_pool_failover_and_topup(fake):
    from webmodder.keys import KeyPool

    _, base = fake
    pool = KeyPool()
    a = pool.add("rl", provider="custom", base=base)
    b = pool.add("good", provider="custom", base=base)
    assert a["model"] == "fake-model" and a["status"] == "active"
    assert pool.acquire()["id"] == a["id"]
    pool.failure(a, "rate_limit", "429", retry_after=60)
    assert pool.acquire()["id"] == b["id"]
    pool.failure(b, "auth", "401")
    assert pool.acquire() is None
    assert 0 < pool.next_ready_in() <= 60
    # pasting the same key again tops it up: re-checked and active again
    pool.add("good", provider="custom", base=base)
    assert pool.acquire()["id"] == b["id"]
    # a token budget: exhausted -> parked, raised -> back
    pool.update(b["id"], budget=100)
    pool.success(b, {"input": 80, "output": 30})
    assert pool.acquire(exclude={a["id"]}) is None
    pool.update(b["id"], budget=1000)
    assert pool.acquire(exclude={a["id"]})["id"] == b["id"]
    # persisted, and secrets never leave public()
    again = KeyPool()
    assert len(again.keys) == 2
    assert all("secret" not in k and "good" not in json.dumps(k) for k in again.public())


def test_quota_is_paused_not_fatal():
    from webmodder.keys import KeyPool

    pool = KeyPool()
    k = pool.add("sk-test", provider="openai", probe=False)
    pool.failure(k, "quota", "insufficient_quota")
    assert k["status"] == "cooldown" and pool.next_ready_in() > 600


# ------------------------------------------------------------------ conversion
def test_openai_conversion_roundtrip():
    from webmodder.providers import _anthropic_messages, _openai_messages

    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "мод для Terraria"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "plan", "signature": "sig", "_provider": "anthropic"},
            {"type": "text", "text": "Сканирую"},
            {"type": "tool_use", "id": "tu_1", "name": "um", "input": {"args": "scan terraria"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "engine: tModLoader",
             "images": [{"media_type": "image/png", "data": "AAAA"}]},
            {"type": "text", "text": "и ещё добавь босса"}]},
    ]
    o = _openai_messages("sys", msgs, "openai")
    assert [m["role"] for m in o] == ["system", "user", "assistant", "tool", "user"]
    assert o[2]["tool_calls"][0]["id"] == "tu_1" and "thinking" not in json.dumps(o)
    assert o[4]["content"][-1]["type"] == "image_url"
    m = _openai_messages("sys", msgs, "mistral")
    assert len(m[2]["tool_calls"][0]["id"]) == 9 and m[3]["tool_call_id"] == m[2]["tool_calls"][0]["id"]
    a = _anthropic_messages(msgs)
    assert a[1]["content"][0] == {"type": "thinking", "thinking": "plan", "signature": "sig"}
    assert a[2]["content"][0]["content"][1]["type"] == "image"


def test_classify():
    from webmodder.providers import _classify

    assert _classify(429, "Rate limit reached") == "rate_limit"
    assert _classify(429, "You exceeded your current quota") == "quota"
    assert _classify(400, "Your credit balance is too low") == "quota"
    assert _classify(401, "invalid x-api-key") == "auth"
    assert _classify(400, "prompt is too long: 1200000 tokens") == "context"
    assert _classify(529, "Overloaded") == "overloaded"
    assert _classify(404, "model not found") == "model"


def test_approvals(tmp_path):
    from webmodder.tools import needs_approval

    wd = tmp_path / "w"
    wd.mkdir()
    assert not needs_approval("um", {"args": "scan --list"}, "smart", wd)
    assert not needs_approval("um", {"args": 'kb search "gta"'}, "smart", wd)
    assert needs_approval("um", {"args": "win drive --proc Terraria click 10 10"}, "smart", wd)
    assert needs_approval("um", {"args": "kb pr note.md --yes"}, "smart", wd)
    assert not needs_approval("write_file", {"path": "Mod/Items.cs"}, "smart", wd)
    assert needs_approval("write_file", {"path": str(tmp_path / "Game" / "x.dll")}, "smart", wd)
    assert not needs_approval("shell", {"command": "ls -la"}, "smart", wd)
    assert needs_approval("shell", {"command": "rm -rf build"}, "smart", wd)
    assert needs_approval("shell", {"command": "ls; rm x"}, "smart", wd)
    assert not needs_approval("shell", {"command": "rm -rf build"}, "auto", wd)
    assert needs_approval("um", {"args": "scan --list"}, "ask", wd)


def test_compaction_keeps_alternation():
    from webmodder.agent import _cut, _trim

    msgs = [{"role": "user", "content": [{"type": "text", "text": "задача"}]}]
    for i in range(30):
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "shell", "input": {}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "x" * 5000}]})
    t = _trim(msgs, keep_last=4, max_result=100, drop_images=True)
    assert len(t) == len(msgs) and len(t[2]["content"][0]["content"]) < 200 and len(t[-1]["content"][0]["content"]) == 5000
    c = _cut(msgs, keep=6)
    roles = [m["role"] for m in c]
    assert roles[0] == "user" and roles[1] == "assistant"
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
    assert "MODLOG.md" in c[0]["content"][0]["text"]


# ------------------------------------------------------------------ the whole loop
def _toolkit_ready():
    from webmodder import toolkit

    return toolkit.is_ready()


@pytest.mark.skipif(not os.environ.get("WM_TEST_TOOLKIT"), reason="set WM_TEST_TOOLKIT to a universal-modder clone")
def test_agent_runs_through_failover(fake, tmp_path):
    from webmodder.agent import Manager
    from webmodder.keys import KeyPool

    f, base = fake
    pool = KeyPool()
    pool.add("rl", provider="custom", base=base, label="rate-limited")
    pool.add("broke", provider="custom", base=base, label="no-credit")
    f.script = [
        {"content": "Сначала база знаний.", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "um", "arguments": json.dumps({"args": "kb search terraria --limit 1"})}},
            {"id": "c2", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "MODLOG.md", "content": "# log"})}}]},
        {"content": None, "tool_calls": [
            {"id": "c3", "type": "function", "function": {"name": "shell", "arguments": json.dumps({"command": "rm -rf /tmp/nothing-here"})}}]},
        {"content": "Мод готов."},
    ]
    m = Manager(pool)
    s = m.create("Добавь ракетницу", game="Terraria")
    # no working key yet: the session waits instead of failing
    deadline = time.time() + 10
    while s.d["status"] != "waiting_keys" and time.time() < deadline:
        time.sleep(0.1)
    assert s.d["status"] == "waiting_keys"
    pool.add("good", provider="custom", base=base, label="working")  # top up -> carries on by itself
    deadline = time.time() + 30
    while s.d["status"] != "waiting_approval" and time.time() < deadline:
        time.sleep(0.1)
    assert s.d["status"] == "waiting_approval" and s.d["pending"]["name"] == "shell"
    m.decide(s.id, "deny", "не удаляй ничего")
    deadline = time.time() + 30
    while s.d["status"] != "done" and time.time() < deadline:
        time.sleep(0.1)
    assert s.d["status"] == "done", [e for e in s.events if e["type"] in ("error", "status")]
    types = [e["type"] for e in s.events]
    assert "key_error" in types and "wait" in types and "approval" in types
    assert (Path(s.d["workdir"]) / "MODLOG.md").read_text() == "# log"
    results = [b for msg in s.d["messages"] for b in msg["content"] if b.get("type") == "tool_result"]
    assert "запретил" in results[-1]["content"]
    assert not results[0]["is_error"], results[0]["content"]
    # the working key got the tool results back in OpenAI shape
    last_body = [b for k, b in f.calls if k == "good"][-1]
    assert any(x["role"] == "tool" for x in last_body["messages"])
    assert s.d["usage"]["requests"] == 3
    # survives a restart: a fresh manager loads it as done
    assert Manager(pool).get(s.id).d["status"] == "done"


def test_http_api(fake):
    import urllib.request

    from webmodder.server import App, make_handler

    app = App()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url + "/api/state")
        assert e.value.code == 401
        h = {"X-WM-Token": app.token, "Content-Type": "application/json"}
        st = json.load(urllib.request.urlopen(urllib.request.Request(url + "/api/state", headers=h)))
        assert st["keys"] == [] and "anthropic" in st["providers"]
        _, base = fake
        req = urllib.request.Request(url + "/api/keys", data=json.dumps({"secret": "good", "base": base}).encode(),
                                     headers=h, method="POST")
        r = json.load(urllib.request.urlopen(req))
        assert len(r["added"]) == 1 and r["keys"][0]["masked"] != "good"
        page = urllib.request.urlopen(urllib.request.Request(url + "/", headers=h)).read().decode()
        assert app.token in page and "Web Modder" in page
        login = urllib.request.urlopen(url + "/").read().decode()
        assert app.token not in login
    finally:
        httpd.shutdown()
