"""The agent's hands: shell, files, the `um` CLI, skills, images, web pages and questions to the user.

Each tool is a JSON-schema definition plus an executor. `needs_approval()` applies the toolkit's own "ask
first" rules (driving input, launching or killing games, registry, installing into game folders, publishing).
"""
from __future__ import annotations

import base64
import html
import io
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from webmodder import toolkit

MAX_OUT = 24000
IS_WIN = os.name == "nt"


def shell_name() -> str:
    if IS_WIN:
        return "powershell"
    return "bash" if shutil.which("bash") else "sh"


DEFS = [
    {"name": "shell",
     "description": f"Run a command in {shell_name()} in the session's working folder. `um` (the universal-modder CLI) "
                    "is on PATH. Output is truncated to the first and last ~12k characters. Use background=true for "
                    "programs that keep running (a game, a server, a recorder): it returns the PID and a log path.",
     "parameters": {"type": "object", "properties": {
         "command": {"type": "string"},
         "timeout": {"type": "integer", "description": "seconds, default 600, max 7200"},
         "cwd": {"type": "string", "description": "working directory (default: the session folder)"},
         "background": {"type": "boolean"}}, "required": ["command"]}},
    {"name": "um",
     "description": "Run the universal-modder CLI: `um <args>`. Examples: 'scan --list', 'scan terraria --json', "
                    "'kb search \"grand theft auto\"', 'fal sprite \"a homing missile launcher\" -o art/', "
                    "'backup create \"<saves>\" --name game-saves', 'win shot --proc Terraria --scale 0.33 -o shot.png', "
                    "'publish check ./MyMod --game \"<install>\"'. Every group has --help.",
     "parameters": {"type": "object", "properties": {
         "args": {"type": "string", "description": "everything after `um`"},
         "timeout": {"type": "integer"}}, "required": ["args"]}},
    {"name": "read_file",
     "description": "Read a text file (relative to the session folder, or absolute). Returns numbered lines.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "offset": {"type": "integer", "description": "first line, 1-based"},
         "limit": {"type": "integer", "description": "lines, default 1500"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Create or overwrite a file with the given content (parent folders are created).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]}},
    {"name": "edit_file",
     "description": "Replace an exact, unique snippet of a file with new text. Set replace_all to change every occurrence.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
         "replace_all": {"type": "boolean"}}, "required": ["path", "old", "new"]}},
    {"name": "list_dir", "description": "List a folder (depth 1-3).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}},
                    "required": ["path"]}},
    {"name": "read_skill",
     "description": "Read a universal-modder skill or reference. name = a skill (mod-any-game, game-recon, "
                    "reverse-engineering, fal-assets, asset-pipeline, game-automation, showcase-video, mashup-mods, "
                    "publish-mod, share-field-notes, game-research-websearch). file = a path inside it, e.g. "
                    "'references/engines/unity.md', 'references/safety.md'. With name='' file is relative to the "
                    "toolkit root (e.g. 'examples/terraria-tmodloader/README.md', 'knowledge/INDEX.md').",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "file": {"type": "string"}},
                    "required": ["name"]}},
    {"name": "view_image",
     "description": "Look at an image file (a screenshot from `um win shot`, a generated sprite, a contact sheet).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                     "max_size": {"type": "integer", "description": "px, default 1280"}},
                    "required": ["path"]}},
    {"name": "fetch_url", "description": "GET a web page or API (modding wikis, GitHub, Nexus, loader releases); returns text.",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "ask_user",
     "description": "Ask the user a question and wait for the answer: confirm ownership/offline play, choose between "
                    "routes, permission to install a loader, the game's path, anything only they know.",
     "parameters": {"type": "object", "properties": {
         "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
         "required": ["question"]}},
]

_SAFE_UM = re.compile(r"^\s*(--help|-h|--version|scan\b|kb\s+(search|show|check|index|new|sync)\b|fal\b|comfy\b|sprite\b|"
                      r"render3d\b|video\b|backup\s+(create|diff|list)\b|publish\s+check\b|win\s+(shot|ps)\b|\S+\s+--help)")
_SAFE_SHELL = re.compile(r"^\s*(ls|dir|pwd|cat|type|head|tail|wc|file|stat|which|where|echo|find|grep|rg|tree|"
                         r"git\s+(status|log|diff|show|branch)|python3?\s+--version|dotnet\s+--(info|version)|"
                         r"Get-ChildItem|Get-Content|Get-Item|Test-Path|Resolve-Path|um\s+.*)\b")


def needs_approval(name: str, args: dict, mode: str, workdir: Path) -> bool:
    if mode == "auto" or name in ("read_file", "list_dir", "read_skill", "view_image", "ask_user"):
        return False
    if mode == "ask":
        return True
    # "smart": the toolkit's ask-first list, everything else runs
    if name == "fetch_url":
        return False
    if name in ("write_file", "edit_file"):
        p = resolve(args.get("path", ""), workdir)
        return workdir.resolve() not in p.parents
    if name == "um":
        return not _SAFE_UM.match(args.get("args", ""))
    if name == "shell":
        cmd = args.get("command", "")
        if re.search(r"[;&|<>`]|\$\(", cmd) or args.get("background"):
            return True
        m = _SAFE_SHELL.match(cmd)
        if m and cmd.strip().startswith("um "):
            return not _SAFE_UM.match(cmd.strip()[3:])
        return not m
    return True


def resolve(path: str, workdir: Path) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(path or ".")))
    return (p if p.is_absolute() else workdir / p).resolve()


def _clip(text: str) -> str:
    if len(text) <= MAX_OUT:
        return text
    h = MAX_OUT // 2
    return text[:h] + f"\n\n… [пропущено {len(text) - MAX_OUT} символов] …\n\n" + text[-h:]


def split_args(s: str) -> list[str]:
    """Like a shell, but Windows paths keep their backslashes."""
    if IS_WIN or "\\" in s:
        parts = shlex.split(s, posix=False)
        return [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return shlex.split(s)


class Runner:
    """Executes tool calls for one session."""

    def __init__(self, workdir: Path, fal_key_fn, on_output=None, stop_flag=None):
        self.workdir = workdir
        self.fal_key_fn = fal_key_fn
        self.on_output = on_output or (lambda text: None)
        self.stop_flag = stop_flag or threading.Event()
        self.bg_count = 0

    # returns (text, images, is_error)
    def run(self, name: str, args: dict) -> tuple[str, list[dict], bool]:
        if "_raw" in args:
            return f"аргументы не являются корректным JSON: {args['_raw'][:500]}. Повтори вызов с правильным JSON.", [], True
        fn = getattr(self, "t_" + name, None)
        if not fn:
            return f"нет такого инструмента: {name}", [], True
        try:
            return fn(**args)
        except TypeError as e:
            return f"неверные аргументы для {name}: {e}", [], True
        except Exception as e:  # a tool failure goes back to the model, never kills the session
            return f"{type(e).__name__}: {e}", [], True

    # ------------------------------------------------------------------ processes
    def _exec(self, argv: list[str] | str, timeout: int, cwd: Path, use_shell: bool) -> tuple[str, list, bool]:
        timeout = max(5, min(int(timeout or 600), 7200))
        env = toolkit.env(self.fal_key_fn())
        kw = {"cwd": str(cwd), "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
        if IS_WIN:
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        if use_shell:
            argv = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", argv] if IS_WIN
                    else [shell_name(), "-c", argv])
        proc = subprocess.Popen(argv, **kw)
        chunks: list[str] = []
        last_emit = [time.time()]

        def reader():
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace")
                chunks.append(line)
                if time.time() - last_emit[0] > 0.7:
                    last_emit[0] = time.time()
                    self.on_output("".join(chunks)[-3000:])

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        start, killed = time.time(), ""
        while proc.poll() is None:
            if self.stop_flag.is_set():
                killed = "остановлено пользователем"
            elif time.time() - start > timeout:
                killed = f"таймаут {timeout} с"
            if killed:
                self._kill(proc)
                break
            time.sleep(0.2)
        t.join(timeout=5)
        out = "".join(chunks)
        code = proc.returncode
        tail = f"\n[код выхода {code}]" + (f" [{killed}]" if killed else "")
        return _clip(out) + tail, [], bool(killed) or code != 0

    @staticmethod
    def _kill(proc):
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()

    def t_shell(self, command: str, timeout: int = 600, cwd: str | None = None, background: bool = False):
        wd = resolve(cwd, self.workdir) if cwd else self.workdir
        if background:
            self.bg_count += 1
            logp = self.workdir / ".wm" / f"bg-{int(time.time())}-{self.bg_count}.log"
            logp.parent.mkdir(parents=True, exist_ok=True)
            f = open(logp, "wb")
            argv = (["powershell", "-NoProfile", "-Command", command] if IS_WIN else [shell_name(), "-c", command])
            kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WIN else {"start_new_session": True}
            p = subprocess.Popen(argv, cwd=str(wd), env=toolkit.env(self.fal_key_fn()), stdout=f, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, **kw)
            return f"запущено в фоне: PID {p.pid}, лог {logp}", [], False
        return self._exec(command, timeout, wd, use_shell=True)

    def t_um(self, args: str, timeout: int = 1800):
        return self._exec(toolkit.um_argv(split_args(args)), timeout, self.workdir, use_shell=False)

    # ------------------------------------------------------------------ files
    def t_read_file(self, path: str, offset: int = 1, limit: int = 1500):
        p = resolve(path, self.workdir)
        data = p.read_bytes()
        if b"\0" in data[:4096]:
            return f"{p}: двоичный файл, {len(data)} байт", [], True
        lines = data.decode("utf-8", "replace").splitlines()
        offset = max(1, int(offset or 1))
        sel = lines[offset - 1: offset - 1 + max(1, int(limit or 1500))]
        body = "\n".join(f"{i:>6}\t{l}" for i, l in enumerate(sel, offset))
        more = f"\n[всего строк: {len(lines)}]" if len(lines) > offset - 1 + len(sel) else ""
        return _clip(body) + more, [], False

    def t_write_file(self, path: str, content: str):
        p = resolve(path, self.workdir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"записано {p} ({len(content)} символов)", [], False

    def t_edit_file(self, path: str, old: str, new: str, replace_all: bool = False):
        p = resolve(path, self.workdir)
        text = p.read_text(encoding="utf-8")
        n = text.count(old)
        if n == 0:
            return "фрагмент не найден — прочитай файл и повтори с точным текстом", [], True
        if n > 1 and not replace_all:
            return f"фрагмент встречается {n} раз — добавь контекст или replace_all=true", [], True
        p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1), encoding="utf-8")
        return f"изменено {p} ({n if replace_all else 1} замен)", [], False

    def t_list_dir(self, path: str = ".", depth: int = 1):
        root = resolve(path, self.workdir)
        depth = max(1, min(int(depth or 1), 3))
        out = []

        def walk(d: Path, level: int):
            try:
                entries = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            except OSError as e:
                out.append("  " * level + f"[{e}]")
                return
            for e in entries:
                if len(out) >= 600:
                    return
                if e.is_dir():
                    out.append("  " * level + e.name + "/")
                    if level + 1 < depth and e.name not in (".git", "node_modules", "__pycache__", "bin", "obj"):
                        walk(e, level + 1)
                else:
                    try:
                        size = e.stat().st_size
                    except OSError:
                        size = 0
                    out.append("  " * level + f"{e.name}  ({size} B)")

        walk(root, 0)
        return f"{root}\n" + "\n".join(out), [], False

    def t_read_skill(self, name: str, file: str | None = None):
        return _clip(toolkit.read_doc(name, file)), [], False

    def t_view_image(self, path: str, max_size: int = 1280):
        from PIL import Image

        p = resolve(path, self.workdir)
        im = Image.open(p)
        im.thumbnail((int(max_size or 1280),) * 2)
        buf = io.BytesIO()
        if im.mode in ("RGBA", "LA", "P"):
            im.convert("RGBA").save(buf, "PNG")
            mt = "image/png"
        else:
            im.convert("RGB").save(buf, "JPEG", quality=85)
            mt = "image/jpeg"
        return f"{p.name}: {im.width}x{im.height}", [{"media_type": mt, "data": base64.b64encode(buf.getvalue()).decode()}], False

    def t_fetch_url(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 web-modder"})
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read(3_000_000)
            ctype = r.headers.get("content-type", "")
        text = raw.decode("utf-8", "replace")
        if "html" in ctype or text.lstrip().startswith("<"):
            text = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", text)
            text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", text)
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n\n", text)
        return _clip(text.strip()), [], False
