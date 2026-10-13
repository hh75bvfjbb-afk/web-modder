"""The local web server: the page, a JSON API and a live event stream per session.

Binds to 127.0.0.1 and requires an access token (the toolkit's rule for any bridge it adds), because whoever
can open this page can run commands on this PC.
"""
from __future__ import annotations

import json
import mimetypes
import subprocess
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from webmodder import toolkit, tools
from webmodder.agent import Manager
from webmodder.config import access_token
from webmodder.keys import PROVIDERS, KeyPool, _http_json, discover_freellmapi, list_models

STATIC = Path(__file__).parent / "static"


class App:
    def __init__(self):
        self.token = access_token()
        self.pool = KeyPool()
        self.manager = Manager(self.pool)
        self._games: tuple[float, object] = (0, None)
        self._fl: tuple[float, str] = (0, "")

    def freellmapi_running(self) -> str:
        """Checked at most once a minute, in the background, so /api/state never waits on it."""
        if time.time() - self._fl[0] > 60:
            self._fl = (time.time(), self._fl[1])

            def probe():
                self._fl = (time.time(), discover_freellmapi())

            threading.Thread(target=probe, daemon=True).start()
        return self._fl[1]

    def router_status(self, key: dict) -> dict:
        """FreeLLMAPI's own view, read with the unified key: upstream providers and quota pools."""
        base = (key.get("base") or PROVIDERS["freellmapi"]["base"]).rstrip("/")
        h = {"Authorization": "Bearer " + key["secret"]}
        st1, prov, err1 = _http_json(base + "/providers", h, timeout=8)
        st2, fc, err2 = _http_json(base + "/quota-forecast", h, timeout=8)
        return {"ok": st1 == 200, "error": "" if st1 == 200 else f"HTTP {st1} {err1[:200]}",
                "providers": (prov or {}).get("providers", []), "counts": (prov or {}).get("counts", {}),
                "pools": (fc or {}).get("pools", []) if st2 == 200 else []}

    def games(self, refresh: bool = False):
        if not refresh and self._games[1] is not None and time.time() - self._games[0] < 300:
            return self._games[1]
        r = subprocess.run(toolkit.um_argv(["scan", "--list", "--json"]), capture_output=True, text=True,
                           env=toolkit.env(), timeout=180)
        try:
            games = json.loads(r.stdout or "[]")
        except ValueError:
            games = []
        self._games = (time.time(), games)
        return games


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "web-modder"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # ------------------------------------------------------------ helpers
        def _authed(self, qs) -> bool:
            if self.headers.get("X-WM-Token") == app.token or (qs.get("token") or [""])[0] == app.token:
                return True
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return "wm_token" in c and c["wm_token"].value == app.token

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

        def _static(self, name: str):
            p = (STATIC / name).resolve()
            if STATIC.resolve() not in p.parents or not p.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            body = p.read_bytes()
            if name == "index.html":  # only ever served to an authenticated browser
                body = body.replace(b"__WM_TOKEN__", app.token.encode())
            self._send(200, body, ctype)

        # ------------------------------------------------------------ routing
        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

        def _route(self, method: str):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            parts = [p for p in u.path.split("/") if p]
            try:
                if method == "GET" and not parts:
                    if (qs.get("token") or [""])[0] == app.token:
                        return self._send(302, b"", "text/plain", {
                            "Location": "/", "Set-Cookie": f"wm_token={app.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"})
                    return self._static("index.html" if self._authed(qs) else "login.html")
                if method == "GET" and parts[0] == "static":
                    return self._static("/".join(parts[1:]))
                if parts[0] != "api":
                    return self._send(404, b"not found", "text/plain")
                if not self._authed(qs):
                    return self._json({"error": "нужен токен доступа"}, 401)
                # CSRF: state-changing calls must come from our own page
                if method != "GET" and self.headers.get("X-WM-Token") != app.token:
                    return self._json({"error": "нет заголовка X-WM-Token"}, 403)
                return self._api(method, parts[1:], qs)
            except (KeyError, FileNotFoundError) as e:
                return self._json({"error": f"не найдено: {e}"}, 404)
            except (ValueError, PermissionError) as e:
                return self._json({"error": str(e)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception as e:
                traceback.print_exc()
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def _api(self, method: str, p: list[str], qs: dict):
            m = app.manager
            pool = app.pool
            if p == ["state"] and method == "GET":
                return self._json({"keys": pool.public(), "keys_summary": pool.summary(),
                                   "sessions": sorted((s.summary() for s in m.sessions.values()),
                                                      key=lambda x: -(x.get("updated") or 0)),
                                   "providers": {k: {"name": v["name"], "kind": v["kind"]} for k, v in PROVIDERS.items()},
                                   "toolkit": {"ready": toolkit.is_ready(), "version": toolkit.version() if toolkit.is_ready() else None,
                                               "path": str(toolkit.toolkit_dir())},
                                   "shell": tools.shell_name(),
                                   "freellmapi_running": app.freellmapi_running(),  # origin or ""
                                   "freellmapi_added": any(k["provider"] == "freellmapi" for k in pool.keys)})
            # ------------------------------------------------ keys
            if p == ["keys"] and method == "POST":
                b = self._body()
                added, errors = [], []
                secrets = [s.strip() for s in (b.get("secret") or "").replace(",", "\n").splitlines() if s.strip()]
                if not secrets and b.get("base"):
                    secrets = [""]
                for sec in secrets:
                    try:
                        k = pool.add(sec, provider=b.get("provider"), base=b.get("base") or None,
                                     model=b.get("model") or None, label=b.get("label") or None)
                        if b.get("budget"):
                            pool.update(k["id"], budget=b["budget"])
                        added.append(k["id"])
                    except ValueError as e:
                        errors.append(f"{sec[:8]}…: {e}")
                return self._json({"added": added, "errors": errors, "keys": pool.public()})
            if len(p) >= 2 and p[0] == "keys":
                kid = p[1]
                if method == "PATCH":
                    pool.update(kid, **self._body())
                    return self._json({"keys": pool.public()})
                if method == "DELETE":
                    pool.remove(kid)
                    return self._json({"keys": pool.public()})
                if method == "POST" and p[2:] == ["check"]:
                    pool.recheck(kid)
                    return self._json({"keys": pool.public()})
                if method == "GET" and p[2:] == ["router"]:
                    k = pool.get(kid)
                    if not k or k["provider"] != "freellmapi":
                        raise ValueError("это не ключ FreeLLMAPI")
                    return self._json(app.router_status(k))
                if method == "GET" and p[2:] == ["models"]:
                    k = pool.get(kid)
                    status, ids, err = list_models(k["provider"], k["secret"], k.get("base") or None)
                    if ids:
                        k["models"] = ids[:300]
                        pool.save()
                    return self._json({"status": status, "models": ids, "error": err})
            # ------------------------------------------------ sessions
            if p == ["sessions"] and method == "POST":
                b = self._body()
                if not (b.get("prompt") or "").strip():
                    raise ValueError("опишите, что сделать")
                s = m.create(b["prompt"], title=b.get("title"), workdir=b.get("workdir") or None,
                             mode=b.get("mode") or "smart", effort=b.get("effort") or "high", game=b.get("game") or None)
                return self._json(s.summary())
            if len(p) >= 2 and p[0] == "sessions":
                s = m.get(p[1])
                sub = p[2:]
                if method == "GET" and not sub:
                    return self._json({**s.summary(), "events": [e for e in s.events if not e.get("ephemeral")],
                                       "effort": s.d.get("effort")})
                if method == "DELETE" and not sub:
                    m.delete(s.id)
                    return self._json({"ok": True})
                if method == "GET" and sub == ["stream"]:
                    return self._stream(s, int((qs.get("after") or ["0"])[0]))
                if method == "GET" and sub == ["files"]:
                    return self._files(s, (qs.get("path") or [""])[0])
                if method == "GET" and sub == ["raw"]:
                    return self._raw(s, (qs.get("path") or [""])[0])
                if method == "POST":
                    b = self._body()
                    if sub == ["message"]:
                        m.message(s.id, b.get("text", "").strip() or "Продолжай")
                    elif sub == ["approve"]:
                        m.decide(s.id, b.get("decision", "allow"), b.get("note", ""))
                    elif sub == ["answer"]:
                        m.answer(s.id, b.get("text", ""))
                    elif sub == ["stop"]:
                        m.stop(s.id)
                    elif sub == ["resume"]:
                        m.resume(s.id)
                    elif sub == ["settings"]:
                        m.settings(s.id, **b)
                    else:
                        raise KeyError("/".join(sub))
                    return self._json(s.summary())
            # ------------------------------------------------ toolkit
            if p == ["assets", "status"] and method == "GET":
                from webmodder import assets

                return self._json(assets.status(assets.Ctx(Path.home(), pool)))
            if p == ["games"] and method == "GET":
                return self._json({"games": app.games(refresh="refresh" in qs)})
            if p == ["kb"] and method == "GET":
                q = (qs.get("q") or [""])[0]
                r = subprocess.run(toolkit.um_argv(["kb", "search", *q.split(), "--json", "--limit", "15"]),
                                   capture_output=True, text=True, env=toolkit.env(), timeout=120)
                try:
                    res = json.loads(r.stdout or "[]")
                except ValueError:
                    res = []
                return self._json({"results": res, "error": r.stderr[-400:] if r.returncode else ""})
            if p == ["toolkit", "update"] and method == "POST":
                return self._json({"output": toolkit.update(), "version": toolkit.version()})
            raise KeyError("/".join(p))

        def _stream(self, s, after: int):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.close_connection = True
            last_ping = time.time()
            while True:
                with s.cond:
                    evs = s.events_after(after)
                    if not evs:
                        s.cond.wait(15)
                        evs = s.events_after(after)
                try:
                    for e in evs:
                        self.wfile.write(f"id: {e['seq']}\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n".encode())
                        after = e["seq"]
                    if not evs and time.time() - last_ping > 14:
                        self.wfile.write(b": ping\n\n")
                        last_ping = time.time()
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

        def _inside(self, s, rel: str) -> Path:
            root = Path(s.d["workdir"]).resolve()
            p = (root / rel).resolve()
            if p != root and root not in p.parents:
                raise PermissionError("путь вне папки сессии")
            return p

        def _files(self, s, rel: str):
            p = self._inside(s, rel)
            items = []
            if p.is_dir():
                for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:500]:
                    st = e.stat()
                    items.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size, "mtime": st.st_mtime,
                                  "path": e.relative_to(Path(s.d["workdir"]).resolve()).as_posix()})
            return self._json({"path": rel, "root": s.d["workdir"], "items": items})

        def _raw(self, s, rel: str):
            p = self._inside(s, rel)
            if not p.is_file():
                raise FileNotFoundError(rel)
            ctype = mimetypes.guess_type(p.name)[0] or "text/plain"
            if ctype.startswith("text/") or p.suffix.lower() in (".md", ".json", ".cs", ".py", ".log", ".txt", ".hjson"):
                ctype = "text/plain; charset=utf-8"
            if ctype in ("text/html", "image/svg+xml"):
                ctype = "text/plain; charset=utf-8"  # never render workspace HTML/SVG on our origin
            self._send(200, p.read_bytes(), ctype, {"Content-Disposition": f"inline; filename*=UTF-8''{quote(p.name)}"})

    return H


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    app = App()
    if not toolkit.is_ready():
        threading.Thread(target=toolkit.ensure, daemon=True).start()
    app.manager.resume_all()
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/?token={app.token}"
    print(f"\n  Web Modder запущен: {url}\n  (ссылка с токеном — не публикуйте её; окно не закрывайте, пока работаете)\n", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("  ВНИМАНИЕ: сервер доступен из сети. Любой, у кого есть токен, может выполнять команды на этом ПК.\n")
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("остановлено")
