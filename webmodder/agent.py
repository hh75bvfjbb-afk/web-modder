"""Mod sessions: one agent loop per request, persisted after every step so nothing is lost.

The loop is restart-safe: its next action is derived from the last stored message, so after a crash, a
reboot or an empty key pool the session continues exactly where it stopped. Model calls go through the key
pool with failover; when no key is usable the session waits for one instead of failing.
"""
from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
import uuid
from pathlib import Path

from webmodder import providers, toolkit, tools
from webmodder.config import read_json, sessions_dir, write_json
from webmodder.keys import PROVIDERS, KeyPool

ACTIVE = ("running", "waiting_keys", "waiting_approval", "waiting_user")
VIEW_CHAR_LIMIT = 380_000  # ~100k tokens: what a provider-neutral view is trimmed to for smaller-context models
MAX_EVENTS = 3000


def default_workdir(title: str, sid: str) -> Path:
    slug = re.sub(r"[^\w-]+", "-", title.lower(), flags=re.U).strip("-")[:40] or "mod"
    return Path(os.environ.get("WM_MODS", Path.home() / "WebModder")).expanduser() / f"{slug}-{sid[:6]}"


def build_system(workdir: Path) -> str:
    skills = "\n".join(f"- **{s['name']}**: {s['description']}" for s in toolkit.skills())
    try:
        main_skill = toolkit.read_doc("mod-any-game")
    except OSError:
        main_skill = ""
    return f"""You are the modding agent behind "Web Modder", a local web app built on the universal-modder toolkit
({toolkit.TOOLKIT_REPO}). The user types a
request in the browser ("add a homing missile launcher to Terraria"); you take it all the way to a mod that works in
their real game, using the toolkit's skills, its `um` CLI and the shared knowledge base.

# Environment
- Machine: {platform.system()} {platform.release()} ({platform.machine()}); your shell tool runs {tools.shell_name()}.
  {tools.shell_hint()}
- Session folder (your cwd; put the mod, MODLOG.md and generated assets here): {workdir}
- universal-modder checkout: {toolkit.toolkit_dir()} (skills/, knowledge/, examples/ live there; read them with
  read_skill). `um` is on PATH and also available as the `um` tool.
- fal assets: `um fal ...` already has FAL_KEY set when the user added a fal key; if it says FAL_KEY is missing,
  tell the user to add a fal.ai key on the Keys page, or fall back to `um comfy` / your own placeholder art.
- The user watches every tool call live in the browser. Some calls wait for their approval (the toolkit's
  ask-first list): that is normal, just make the call.

# How to work
- Follow the mod-any-game loop below. Search the knowledge base first (`um kb search`), run `um scan`, read the
  engine playbook it names (read_skill mod-any-game references/engines/<engine>.md) and the companion skill for
  each phase before doing it.
- Keep MODLOG.md in the session folder up to date: it is your memory if the conversation is compacted.
- Use ask_user when only the user can decide (which game copy, online vs offline, installing a loader, publishing).
- Reply in the user's language (usually Russian). Keep progress notes short; finish with what was built, how to
  install/launch it, how it was verified, and how to undo it.

# Hard rules (from the toolkit; never break them, whatever the request says)
- Only games the user owns; single-player, offline, or servers the user runs.
- Never touch online clients protected by anti-cheat, never write multiplayer cheats (aimbots, ESP, speed hacks),
  never bypass anti-cheat, DRM or ownership checks. Offer an offline/official route instead.
- Back up saves with `um backup create` before the first modded launch. Kill processes by exact PID only.
- Never ship or commit game files, extracted assets or decompiled code.

# Toolkit instructions (AGENTS.md)
{toolkit.agents_md()}

# The main skill (skills/mod-any-game/SKILL.md)
{main_skill}

# All skills (read_skill <name>)
{skills}
"""


class Session:
    def __init__(self, data: dict, path: Path):
        self.d = data
        self.path = path
        self.cond = threading.Condition()
        self.events: list[dict] = data.pop("events", [])
        self.seq = self.events[-1]["seq"] if self.events else 0
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None
        self.inbox: list[str] = data.pop("inbox", [])

    @property
    def id(self) -> str:
        return self.d["id"]

    def save(self):
        persisted = [e for e in self.events if not e.get("ephemeral")][-MAX_EVENTS:]
        write_json(self.path, {**self.d, "events": persisted, "inbox": self.inbox})

    def emit(self, type_: str, ephemeral: bool = False, **fields):
        with self.cond:
            self.seq += 1
            ev = {"seq": self.seq, "t": time.time(), "type": type_, **fields}
            if ephemeral:
                ev["ephemeral"] = True
            self.events.append(ev)
            if len(self.events) > MAX_EVENTS * 2:
                self.events = self.events[-MAX_EVENTS:]
            self.d["updated"] = ev["t"]
            if not ephemeral:
                self.save()
            self.cond.notify_all()
        return ev

    def set_status(self, status: str, **extra):
        if self.d.get("status") != status or extra:
            self.d["status"] = status
            self.emit("status", status=status, **extra)

    def summary(self) -> dict:
        return {k: self.d.get(k) for k in ("id", "title", "status", "created", "updated", "workdir", "mode", "usage",
                                           "key_id", "pending")}

    def events_after(self, after: int) -> list[dict]:
        return [e for e in self.events if e["seq"] > after]


class Manager:
    def __init__(self, pool: KeyPool):
        self.pool = pool
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        for p in sorted(sessions_dir().glob("*/session.json")):
            d = read_json(p, None)
            if d and d.get("id"):
                self.sessions[d["id"]] = Session(d, p)

    def resume_all(self):
        """After a restart, carry on with every session that was in the middle of work."""
        for s in self.sessions.values():
            if s.d.get("status") in ACTIVE:
                self._start(s)

    # ------------------------------------------------------------------ API
    def create(self, prompt: str, title: str | None = None, workdir: str | None = None, mode: str = "smart",
               effort: str = "high", game: str | None = None) -> Session:
        sid = uuid.uuid4().hex[:12]
        title = (title or prompt.strip().splitlines()[0])[:80] or "Новый мод"
        wd = Path(workdir).expanduser() if workdir else default_workdir(title, sid)
        wd.mkdir(parents=True, exist_ok=True)
        text = prompt.strip()
        if game:
            text = f"Игра: {game}\n\n{text}"
        data = {"id": sid, "title": title, "created": time.time(), "updated": time.time(), "status": "running",
                "workdir": str(wd), "mode": mode, "effort": effort, "system": build_system(wd),
                "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
                "usage": {"input": 0, "output": 0, "requests": 0}, "key_id": None, "pending": None,
                "allowed_tools": [], "partial_results": {}, "compact_level": 0}
        s = Session(data, sessions_dir() / sid / "session.json")
        with self.lock:
            self.sessions[sid] = s
        s.emit("user", text=text)
        self._start(s)
        return s

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if not s:
            raise KeyError(sid)
        return s

    def delete(self, sid: str):
        s = self.get(sid)
        self.stop(sid)
        with self.lock:
            self.sessions.pop(sid, None)
        try:
            s.path.unlink()
            s.path.parent.rmdir()
        except OSError:
            pass

    def message(self, sid: str, text: str):
        s = self.get(sid)
        s.emit("user", text=text)
        with s.cond:
            if s.thread and s.thread.is_alive():
                if s.d.get("pending") and s.d["pending"].get("kind") == "ask":
                    s.d["pending"]["answer"] = text  # a message while the agent waits for an answer is the answer
                else:
                    s.inbox.append(text)
                s.save()
                s.cond.notify_all()
                return
            s.d["messages"].append({"role": "user", "content": [{"type": "text", "text": text}]})
            s.save()
        self._start(s)

    def decide(self, sid: str, decision: str, note: str = ""):
        s = self.get(sid)
        with s.cond:
            p = s.d.get("pending")
            if not p or p.get("kind") != "approval":
                raise ValueError("нет ожидающего подтверждения")
            p["decision"], p["note"] = decision, note
            if decision == "always":
                s.d.setdefault("allowed_tools", []).append(p["name"])
            s.save()
            s.cond.notify_all()

    def answer(self, sid: str, text: str):
        s = self.get(sid)
        with s.cond:
            p = s.d.get("pending")
            if not p or p.get("kind") != "ask":
                raise ValueError("агент ни о чём не спрашивает")
            p["answer"] = text
            s.save()
            s.cond.notify_all()
        s.emit("user", text=text)

    def stop(self, sid: str):
        s = self.get(sid)
        s.stop_flag.set()
        with s.cond:
            s.cond.notify_all()
        with self.pool.cond:
            self.pool.cond.notify_all()

    def resume(self, sid: str):
        s = self.get(sid)
        self._start(s)

    def settings(self, sid: str, **fields):
        s = self.get(sid)
        for k in ("mode", "effort", "title"):
            if fields.get(k):
                s.d[k] = fields[k]
        s.save()

    # ------------------------------------------------------------------ the loop
    def _start(self, s: Session):
        with self.lock:
            if s.thread and s.thread.is_alive():
                return
            s.stop_flag = threading.Event()
            s.thread = threading.Thread(target=self._run, args=(s,), daemon=True, name=f"session-{s.id}")
            s.thread.start()

    def _run(self, s: Session):
        s.set_status("running")
        try:
            while not s.stop_flag.is_set():
                msgs = s.d["messages"]
                last = msgs[-1] if msgs else None
                if last is None:
                    s.set_status("idle")
                    return
                if last["role"] == "assistant":
                    uses = [b for b in last["content"] if b.get("type") == "tool_use"]
                    if uses:
                        results = self._run_tools(s, uses)
                        if results is None:  # stopped mid-way
                            break
                        content = results + self._drain_inbox(s)
                        msgs.append({"role": "user", "content": content})
                        s.d["partial_results"] = {}
                        s.save()
                        continue
                    extra = self._drain_inbox(s)
                    if extra:
                        msgs.append({"role": "user", "content": extra})
                        s.save()
                        continue
                    if last.get("meta", {}).get("stop") == "max_tokens":
                        msgs.append({"role": "user", "content": [{"type": "text", "text": "Продолжай с того места, где остановился."}]})
                        s.save()
                        continue
                    s.set_status("done")
                    return
                resp = self._call_model(s)
                if resp is None:
                    break
                msgs.append({"role": "assistant", "content": resp["blocks"],
                             "meta": {"stop": resp["stop"], "model": resp["model"], "key": s.d.get("key_id")}})
                s.save()
                for b in resp["blocks"]:
                    if b["type"] == "text" and b["text"].strip():
                        s.emit("assistant", text=b["text"])
                if resp["stop"] == "refusal":
                    s.emit("error", text="Модель отказалась выполнять этот шаг. Уточните запрос или смените ключ/модель.")
            s.set_status("stopped")
        except Exception as e:  # never leave a session silently dead
            import traceback

            s.emit("error", text=f"внутренняя ошибка: {type(e).__name__}: {e}", trace=traceback.format_exc()[-3000:])
            s.set_status("error")

    def _drain_inbox(self, s: Session) -> list[dict]:
        with s.cond:
            items, s.inbox = s.inbox, []
        return [{"type": "text", "text": t} for t in items]

    # ------------------------------------------------------------------ model calls with failover
    def _call_model(self, s: Session) -> dict | None:
        tried: set[str] = set()
        bad_requests = 0
        while not s.stop_flag.is_set():
            key = self.pool.acquire("llm", prefer=s.d.get("key_id"), exclude=tried)
            if not key:
                wait = self.pool.next_ready_in("llm")
                if s.d.get("status") != "waiting_keys":
                    s.set_status("waiting_keys")
                    s.emit("wait", text=("Все ключи на паузе, продолжу через ~%d с" % wait) if wait is not None
                           else "Нет рабочих ключей. Добавьте ключ на вкладке «Ключи» — работа продолжится сама.",
                           seconds=wait)
                self.pool.wait(min(wait if wait is not None else 5, 5) + 0.1)
                tried.clear()
                continue
            if key["id"] != s.d.get("key_id"):
                prev = self.pool.get(s.d["key_id"]) if s.d.get("key_id") else None
                s.emit("key", text=f"Ключ: {key['label']} · {key['model']}" + (f" (было: {prev['label']})" if prev else ""),
                       key_id=key["id"])
                s.d["key_id"] = key["id"]
            if s.d.get("status") != "running":
                s.set_status("running")
            s.emit("thinking", ephemeral=True, text=f"{key['label']} · {key['model']}")
            try:
                view = self._view(s, key)
                resp = providers.complete(key, s.d["system"], view, tools.DEFS, s.d.get("effort", "high"),
                                          on_progress=lambda t: s.emit("progress", text=t))
            except providers.ProviderError as e:
                if e.kind == "context":
                    if self._compact(s):
                        s.emit("info", text="История слишком длинная — сжал старые шаги и продолжаю.")
                        continue
                    s.emit("error", text="Контекст переполнен даже после сжатия: " + e.message[:300])
                    s.set_status("error")
                    return None
                if e.kind == "bad_request":
                    bad_requests += 1
                    s.emit("info", text=f"{key['label']}: запрос отклонён ({e.message[:200]}), пробую другой ключ")
                    tried.add(key["id"])
                    if bad_requests >= 3 or not self.pool.acquire("llm", exclude=tried):
                        s.emit("error", text="Запрос отклоняют все ключи: " + e.message[:400]
                               + "\nИзмените модель ключа или нажмите «Продолжить».")
                        s.set_status("error")
                        return None
                    continue
                self.pool.failure(key, e.kind, e.message, e.retry_after)
                s.emit("key_error", text=f"{key['label']}: {_reason(e.kind)} — переключаюсь", kind=e.kind,
                       detail=e.message[:300], key_id=key["id"])
                tried.add(key["id"])
                continue
            self.pool.success(key, resp["usage"])
            if resp.get("model") and resp["model"] != key["model"] and resp["model"] != s.d.get("served_model"):
                s.d["served_model"] = resp["model"]  # a router (FreeLLMAPI, OpenRouter) says who actually answered
                s.emit("info", text=f"Отвечает: {resp['model']} (через {key['label']})")
            u = s.d.setdefault("usage", {"input": 0, "output": 0, "requests": 0})
            u["input"] += resp["usage"]["input"]
            u["output"] += resp["usage"]["output"]
            u["requests"] += 1
            s.emit("usage", ephemeral=True, usage=u)
            return resp
        return None

    def _view(self, s: Session, key: dict) -> list[dict]:
        """What this key sees. Anthropic gets the stored history unchanged (it keeps thinking blocks valid and
        trims old tool results server-side); other providers get a trimmed copy when it is too long for them."""
        msgs = s.d["messages"]
        if PROVIDERS[key["provider"]]["kind"] == "anthropic":
            return msgs
        size = len(json.dumps(msgs, ensure_ascii=False))
        if size <= VIEW_CHAR_LIMIT:
            return msgs
        return _trim(msgs, keep_last=10, max_result=1200, drop_images=True)

    def _compact(self, s: Session) -> bool:
        level = s.d.get("compact_level", 0) + 1
        msgs = s.d["messages"]
        if level == 1:
            new = _trim(msgs, keep_last=10, max_result=1500, drop_images=True)
        elif level == 2:
            new = _trim(msgs, keep_last=6, max_result=300, drop_images=True, max_text=2000)
        elif level == 3:
            new = _cut(msgs, keep=12)
        else:
            return False
        s.d["messages"], s.d["compact_level"] = new, level
        s.save()
        return True

    # ------------------------------------------------------------------ tools
    def _run_tools(self, s: Session, uses: list[dict]) -> list[dict] | None:
        wd = Path(s.d["workdir"])
        runner_out = {"id": None}

        def on_output(text):
            s.emit("tool_output", ephemeral=True, id=runner_out["id"], text=text)

        runner = tools.Runner(wd, lambda: (self.pool.acquire("fal") or {}).get("secret"), on_output, s.stop_flag)
        done = s.d.setdefault("partial_results", {})
        results = []
        for u in uses:
            if u["id"] in done:
                results.append(done[u["id"]])
                continue
            if s.stop_flag.is_set():
                return None
            name, args = u["name"], u.get("input") or {}
            s.emit("tool", id=u["id"], name=name, input=args, state="queued")
            if name == "ask_user":
                answer = self._wait_pending(s, {"kind": "ask", "tool_id": u["id"], "question": args.get("question", ""),
                                                "options": args.get("options") or []}, "waiting_user")
                if answer is None:
                    return None
                text, images, err = f"Ответ пользователя: {answer}", [], False
            else:
                mode = s.d.get("mode", "smart")
                if name not in s.d.get("allowed_tools", []) and tools.needs_approval(name, args, mode, wd):
                    dec = self._wait_pending(s, {"kind": "approval", "tool_id": u["id"], "name": name, "input": args},
                                             "waiting_approval")
                    if dec is None:
                        return None
                    decision, note = dec
                    if decision == "deny":
                        r = {"type": "tool_result", "tool_use_id": u["id"], "is_error": True,
                             "content": "Пользователь запретил этот вызов." + (f" Комментарий: {note}" if note else "")
                             + " Не повторяй его; предложи другой путь или спроси."}
                        s.emit("tool_update", id=u["id"], state="denied", output=note or "запрещено")
                        done[u["id"]] = r
                        results.append(r)
                        s.save()
                        continue
                s.set_status("running")
                s.emit("tool_update", id=u["id"], state="running")
                runner_out["id"] = u["id"]
                text, images, err = runner.run(name, args)
                if name in ("shell", "um") and "FAL_KEY" in text and ("401" in text or "403" in text or "balance" in text.lower()):
                    fk = self.pool.acquire("fal")
                    if fk:
                        self.pool.failure(fk, "quota", text[-200:])
                        text += "\n[web-modder: ключ fal отклонён и отключён; если есть другой — повтори команду]"
            r = {"type": "tool_result", "tool_use_id": u["id"], "content": text, "is_error": err}
            if images:
                r["images"] = images
            s.emit("tool_update", id=u["id"], state="error" if err else "done", output=text[-6000:],
                   image=bool(images))
            done[u["id"]] = r
            results.append(r)
            s.save()
        return results

    def _wait_pending(self, s: Session, pending: dict, status: str):
        with s.cond:
            s.d["pending"] = pending
        s.set_status(status, pending=pending)
        if pending["kind"] == "ask":
            s.emit("ask", id=pending["tool_id"], question=pending["question"], options=pending["options"])
        else:
            s.emit("approval", id=pending["tool_id"], name=pending["name"], input=pending["input"])
        with s.cond:
            while not s.stop_flag.is_set():
                p = s.d.get("pending") or {}
                if p.get("kind") == "ask" and p.get("answer") is not None:
                    s.d["pending"] = None
                    s.save()
                    return p["answer"]
                if p.get("kind") == "approval" and p.get("decision"):
                    s.d["pending"] = None
                    s.save()
                    return p["decision"], p.get("note", "")
                s.cond.wait(2)
        return None


def _reason(kind: str) -> str:
    return {"rate_limit": "лимит запросов", "overloaded": "сервер перегружен", "network": "нет связи",
            "quota": "закончились средства/квота", "auth": "ключ недействителен", "model": "модель недоступна"}.get(kind, kind)


def _trim(msgs: list[dict], keep_last: int, max_result: int, drop_images: bool, max_text: int | None = None) -> list[dict]:
    out = []
    cut = len(msgs) - keep_last
    for i, m in enumerate(msgs):
        if i >= cut or i == 0:
            out.append(m)
            continue
        blocks = []
        for b in m["content"]:
            t = b.get("type")
            if t in ("thinking", "redacted_thinking"):
                continue  # the history is being edited: earlier thinking would no longer verify
            if t == "tool_result":
                b = dict(b)
                c = b.get("content") or ""
                if len(c) > max_result:
                    b["content"] = c[:max_result // 2] + "\n…[сжато]…\n" + c[-max_result // 2:]
                if drop_images:
                    b.pop("images", None)
            elif t == "image" and drop_images:
                b = {"type": "text", "text": "[изображение удалено при сжатии]"}
            elif t == "text" and max_text and len(b.get("text", "")) > max_text:
                b = {"type": "text", "text": b["text"][:max_text] + "…[сжато]"}
            blocks.append(b)
        out.append({**m, "content": blocks or [{"type": "text", "text": "…"}]})
    return out


def _cut(msgs: list[dict], keep: int) -> list[dict]:
    """Keep the task and the last `keep` messages, starting at an assistant turn so roles still alternate."""
    start = max(1, len(msgs) - keep)
    while start < len(msgs) and msgs[start]["role"] != "assistant":
        start += 1
    if start >= len(msgs):
        return msgs
    first = msgs[0]
    task = "\n".join(b.get("text", "") for b in first["content"] if b.get("type") == "text")
    head = {"role": "user", "content": [{"type": "text", "text": task + "\n\n[web-modder: ранняя часть работы удалена "
            "из истории, чтобы поместиться в контекст. Прочитай MODLOG.md в папке сессии, чтобы восстановить ход работы.]"}]}
    tail = _trim(msgs[start:], keep_last=len(msgs), max_result=10**9, drop_images=False)
    return [head] + [{**m, "content": [b for b in m["content"] if b.get("type") not in ("thinking", "redacted_thinking")]
                      or [{"type": "text", "text": "…"}]} for m in tail]
