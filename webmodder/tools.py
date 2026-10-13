"""The agent's hands: shell, files, the `um` CLI, skills, images, web pages and questions to the user.

Each tool is a JSON-schema definition plus an executor. `needs_approval()` applies the toolkit's own "ask
first" rules (driving input, launching or killing games, registry, installing into game folders, publishing).
"""
from __future__ import annotations

import base64
import html
import json
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


def git_bash() -> str | None:
    """Git for Windows' bash.exe (never System32\\bash.exe, which is WSL and sees a different filesystem)."""
    cands = []
    git = shutil.which("git")
    if git:  # ...\Git\cmd\git.exe -> ...\Git\bin\bash.exe
        cands.append(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if os.environ.get(var):
            cands.append(Path(os.environ[var]) / "Git" / "bin" / "bash.exe")
    if os.environ.get("LOCALAPPDATA"):
        cands.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git" / "bin" / "bash.exe")
    return next((str(c) for c in cands if c.is_file()), None)


_SHELL: list = []


def shell() -> tuple[str, str]:
    """(kind, executable). Models write bash far more reliably than PowerShell, so on Windows Git Bash wins when it
    is installed; then PowerShell 7 (pwsh: knows `&&`, UTF-8 by default); then Windows PowerShell 5.1.
    WM_SHELL=bash|pwsh|powershell forces one."""
    if not _SHELL:
        force = os.environ.get("WM_SHELL", "").lower()
        if not IS_WIN:
            _SHELL.append(("bash", shutil.which("bash")) if shutil.which("bash") else ("sh", "sh"))
        else:
            gb, pw = git_bash(), shutil.which("pwsh")
            if gb and force in ("", "bash"):
                _SHELL.append(("bash", gb))
            elif pw and force in ("", "pwsh"):
                _SHELL.append(("pwsh", pw))
            else:
                _SHELL.append(("powershell", "powershell"))
    return _SHELL[0]


def shell_name() -> str:
    kind = shell()[0]
    return {"bash": "Git Bash (bash on Windows)" if IS_WIN else "bash", "pwsh": "PowerShell 7",
            "powershell": "Windows PowerShell 5.1"}.get(kind, kind)


# PowerShell writes in the console's OEM code page (cp866 on Russian Windows) unless told otherwise
_PS_PREFIX = ("try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}; $OutputEncoding = [Text.Encoding]::UTF8; "
              "$ProgressPreference = 'SilentlyContinue'; ")


def shell_argv(command: str) -> list[str]:
    kind, exe = shell()
    if kind in ("powershell", "pwsh"):
        return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _PS_PREFIX + command]
    return [exe, "-c", command]


def shell_hint() -> str:
    kind = shell()[0]
    if kind == "powershell":
        return ("This is Windows PowerShell 5.1, NOT bash: no `&&`/`||` (use `;` or `if ($?) { ... }`), no heredocs "
                "(`<<`), no `ls -la`/`cat >`/`export`/`rm -rf` (use Get-ChildItem -Force, Remove-Item -Recurse -Force, "
                "$env:NAME='x'). Create and change files only with write_file/edit_file.")
    if kind == "pwsh":
        return ("This is PowerShell 7, NOT bash: no heredocs (`<<`), no `ls -la`/`cat >`/`export` (use Get-ChildItem "
                "-Force, $env:NAME='x'). Create and change files only with write_file/edit_file.")
    if IS_WIN:
        return ("This is Git Bash on Windows: bash syntax works; write Windows paths with forward slashes in quotes "
                "(\"C:/Program Files (x86)/Steam/...\") or as /c/...; Windows programs (dotnet, git, um) run directly. "
                "Prefer write_file/edit_file over heredocs for files.")
    return "Prefer write_file/edit_file over heredocs for files."


_BASHISM = re.compile(r"<<|&&|\bls\s+-\w|\bcat\s+>|\bexport\s+\w+=|\brm\s+-\w|\bgrep\b|\bhead\b|\btail\b|\bwhich\b")


def decode(raw: bytes) -> str:
    """UTF-8, or the Windows OEM/ANSI code page for tools that ignore UTF-8 (cmd, old PowerShell, msbuild)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if IS_WIN:
        import ctypes

        for cp in (ctypes.windll.kernel32.GetOEMCP(), ctypes.windll.kernel32.GetACP()):
            try:
                return raw.decode(f"cp{cp}")
            except (UnicodeDecodeError, LookupError):
                continue
    return raw.decode("utf-8", "replace")


DEFS = [
    {"name": "shell",
     "description": f"Run a command in {shell_name()} in the session's working folder. {shell_hint()} "
                    "`um` (the universal-modder CLI) is on PATH. Output is truncated to the first and last ~12k characters. Use background=true for "
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
    {"name": "make_asset",
     "description": "Make a game asset for FREE (prefer this to `um fal`): it tries free sources in order and says "
                    "which one it used. recipe: sprite (text -> sprite with a transparent background; size='64x64' "
                    "fits it exactly, pixel=true for pixel art), image, texture (seamless tile), rmbg (input image -> "
                    "transparent), upscale (input image, x4), sfx (sound effect; prompt in ENGLISH keywords, e.g. "
                    "'laser shot', 'explosion debris'; source=sfxr gives instant retro 8-bit sounds for pixel-art games), music (prompt = style tags, optional lyrics, seconds), voice "
                    "(prompt = the line to say; voice ru-m/ru-f/en-m/en-f), model3d (input image -> GLB mesh), video, "
                    "status (what is available). Files land in assets/gen unless out is given. Free sources: "
                    "FreeLLMAPI images/speech/video, local ComfyUI (SDXL, Stable Audio, ACE-Step, Hunyuan3D), "
                    "Openverse/Freesound sounds, a built-in sfxr retro synthesizer, edge-tts, local background removal and "
                    "Real-ESRGAN; fal is only a fallback.",
     "parameters": {"type": "object", "properties": {
         "recipe": {"type": "string", "enum": ["status", "sprite", "image", "texture", "rmbg", "upscale", "sfx", "music",
                                               "voice", "model3d", "video"]},
         "prompt": {"type": "string"}, "input": {"type": "string", "description": "input file for rmbg/upscale/model3d"},
         "name": {"type": "string", "description": "file stem"}, "out": {"type": "string"},
         "size": {"type": "string", "description": "final sprite size WxH, or image size"},
         "pixel": {"type": "boolean"}, "colors": {"type": "integer"},
         "seconds": {"type": "number"}, "voice": {"type": "string"}, "lyrics": {"type": "string"},
         "format": {"type": "string", "description": "audio format to convert to (wav/ogg/mp3) when ffmpeg is installed"},
         "source": {"type": "string", "description": "force a source: auto (default), freellmapi, comfy, freesound, openverse, sfxr, edge, fal"}},
         "required": ["recipe"]}},
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
    if mode == "auto" or name in ("read_file", "list_dir", "read_skill", "view_image", "ask_user", "make_asset"):
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

    def __init__(self, workdir: Path, fal_key_fn, on_output=None, stop_flag=None, pool=None):
        self.workdir = workdir
        self.fal_key_fn = fal_key_fn
        self.pool = pool
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
        cmd_text = argv if isinstance(argv, str) else ""
        env = toolkit.env(self.fal_key_fn())
        kw = {"cwd": str(cwd), "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
        if IS_WIN:
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        if use_shell:
            argv = shell_argv(argv)
        proc = subprocess.Popen(argv, **kw)
        chunks: list[str] = []
        last_emit = [time.time()]

        def reader():
            for raw in iter(proc.stdout.readline, b""):
                line = decode(raw)
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
        if use_shell and code and shell()[0] in ("powershell", "pwsh") and _BASHISM.search(cmd_text):
            tail += "\n[web-modder: " + shell_hint() + "]"
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
            argv = shell_argv(command)
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
        lines = decode(data).splitlines()
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

    def t_make_asset(self, recipe: str, **kw):
        from webmodder import assets

        log: list[str] = []

        def note(msg):
            log.append(msg)
            self.on_output("\n".join(log)[-3000:])

        ctx = assets.Ctx(self.workdir, self.pool, note, self.stop_flag)
        kw = {k: v for k, v in kw.items() if k in ("prompt", "input", "name", "out", "size", "pixel", "colors", "seconds",
                                                   "voice", "lyrics", "format", "source", "scale") and v not in (None, "")}
        try:
            res = assets.make(ctx, recipe, **kw)
        except assets.AssetError as e:
            return f"не получилось: {e}", [], True
        if recipe == "status":
            return json.dumps(res["status"], ensure_ascii=False, indent=1), [], False
        lines = [f"готово ({res['source']}):"] + [f"  {f}" for f in res["files"]]
        if res.get("note"):
            lines.append(res["note"])
        if res.get("skipped"):
            lines.append("пропущены: " + "; ".join(res["skipped"]))
        images = []
        last = res["files"][-1]
        if last.lower().endswith(".png"):  # let the model look at what it got
            images = self.t_view_image(last, 512)[1]
        return "\n".join(lines), images, False

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
