"""universal-modder itself: keep a checkout, read its skills, and run its `um` CLI.

The app does not copy the toolkit's logic: the agent reads the real AGENTS.md, skills and engine playbooks
from the checkout and runs the real `um` commands, so updating the checkout (`git pull`) updates the app.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from webmodder.config import TOOLKIT_REPO, toolkit_dir

# Inside WebModder.exe (PyInstaller) there is no python.exe: the exe runs `um` itself (`WebModder.exe --um ...`),
# and a snapshot of universal-modder ships inside it so the first start works offline.
FROZEN = bool(getattr(sys, "frozen", False))
BUNDLED = Path(getattr(sys, "_MEIPASS", "")) / "universal-modder" if FROZEN else None


def is_ready() -> bool:
    d = toolkit_dir()
    return (d / "um" / "cli.py").exists() and (d / "skills").is_dir()


def ensure(log=print) -> Path:
    """Clone universal-modder on first run (git, or a zip download when git is missing)."""
    d = toolkit_dir()
    if is_ready():
        return d
    d.parent.mkdir(parents=True, exist_ok=True)
    if BUNDLED and (BUNDLED / "um" / "cli.py").exists():
        log(f"распаковываю встроенную копию universal-modder -> {d}")
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(BUNDLED, d)
        return d
    if shutil.which("git"):
        log(f"клонирую {TOOLKIT_REPO} -> {d}")
        r = subprocess.run(["git", "clone", "--depth", "1", TOOLKIT_REPO, str(d)], capture_output=True, text=True)
        if r.returncode == 0 and is_ready():
            return d
        log("git clone не удался: " + (r.stderr or r.stdout)[-400:])
    url = TOOLKIT_REPO.rstrip("/") + "/archive/refs/heads/main.zip"
    log(f"скачиваю {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = resp.read()
    tmp = d.with_name(d.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(tmp)
    inner = next(p for p in tmp.iterdir() if p.is_dir())
    shutil.rmtree(d, ignore_errors=True)
    inner.rename(d)
    shutil.rmtree(tmp, ignore_errors=True)
    return d


def update() -> str:
    d = toolkit_dir()
    if (d / ".git").exists() and shutil.which("git"):
        r = subprocess.run(["git", "-C", str(d), "pull", "--ff-only"], capture_output=True, text=True)
        return (r.stdout + r.stderr).strip()
    shutil.rmtree(d, ignore_errors=True)
    ensure()
    return "скачана свежая копия"


def version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (toolkit_dir() / "pyproject.toml").read_text(encoding="utf-8"), re.M) \
        if is_ready() else None
    rev = ""
    if (toolkit_dir() / ".git").exists() and shutil.which("git"):
        rev = subprocess.run(["git", "-C", str(toolkit_dir()), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
    return (m.group(1) if m else "?") + (f" ({rev})" if rev else "")


# ---------------------------------------------------------------------------- skills

def _front_matter(text: str) -> dict:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    out = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
    return out


def skills() -> list[dict]:
    """[{name, description, path}] for every skills/*/SKILL.md."""
    out = []
    for p in sorted((toolkit_dir() / "skills").glob("*/SKILL.md")):
        fm = _front_matter(p.read_text(encoding="utf-8"))
        out.append({"name": fm.get("name", p.parent.name), "description": fm.get("description", ""), "path": p})
    return out


def read_doc(name: str, file: str | None = None) -> str:
    """A skill's SKILL.md, or a file under it (references/engines/unity.md), or any toolkit doc by path."""
    root = toolkit_dir()
    if file:
        base = (root / "skills" / name) if name else root
        p = (base / file).resolve()
    else:
        p = (root / "skills" / name / "SKILL.md").resolve()
        if not p.exists():
            p = (root / name).resolve()
    if root.resolve() not in p.parents and p != root.resolve():
        raise ValueError("путь вне universal-modder")
    if p.is_dir():
        return "\n".join(sorted(x.relative_to(root).as_posix() for x in p.rglob("*") if x.is_file())[:300])
    return p.read_text(encoding="utf-8")


def agents_md() -> str:
    p = toolkit_dir() / "AGENTS.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------------------------------------------------------------------------- running um

def bin_dir() -> Path:
    """A folder with an `um` launcher that works without uv, put first on the agent's PATH.

    The launchers hold no paths: they run "$WM_UM_EXE" from the environment (see env()). A path written into
    the file broke as soon as it had non-ASCII characters (a "D:\\Загрузки\\..." folder): Windows saves the
    file in its ANSI code page, while Git Bash and cmd each read it in a different one.
    """
    d = toolkit_dir().parent / "wm-bin"
    d.mkdir(parents=True, exist_ok=True)
    sh = d / "um"
    sh.write_text('#!/usr/bin/env bash\nexec "$WM_UM_EXE" $WM_UM_ARGS "$@"\n', encoding="ascii", newline="\n")
    try:
        sh.chmod(0o755)
    except OSError:
        pass
    (d / "um.cmd").write_text('@echo off\r\n"%WM_UM_EXE%" %WM_UM_ARGS% %*\r\n', encoding="ascii", newline="")
    return d


def env(fal_key: str | None = None) -> dict:
    e = dict(os.environ)
    e["PATH"] = str(bin_dir()) + os.pathsep + e.get("PATH", "")
    e["PYTHONPATH"] = str(toolkit_dir()) + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    e["PYTHONIOENCODING"] = "utf-8"
    e["WM_UM_EXE"] = sys.executable  # the um launchers run this: WebModder.exe --um, or python -m um
    e["WM_UM_ARGS"] = "--um" if FROZEN else "-m um"
    e["UM_NO_UV"] = "1"
    # English compiler/msbuild messages: models know them far better than localized ones
    e.setdefault("DOTNET_CLI_UI_LANGUAGE", "en")
    e.setdefault("VSLANG", "1033")
    e.setdefault("DOTNET_NOLOGO", "1")
    # Git Bash rewrites args that start with "/" into paths; msbuild switches must survive
    e.setdefault("MSYS2_ARG_CONV_EXCL", "/p:;/t:;/v:;/m:;/m;/nologo;/restore;/property:;/target:")
    for k in [k for k in e if k.endswith("_API_KEY") or k in ("FAL_KEY", "ANTHROPIC_AUTH_TOKEN")]:
        e.pop(k)  # the agent's shell never sees the model keys
    if fal_key:
        e["FAL_KEY"] = fal_key
    return e


def um_argv(args: list[str]) -> list[str]:
    if FROZEN:
        return [sys.executable, "--um", *args]
    return [sys.executable, "-m", "um", *args]


def run_um(args: list[str]):
    """`WebModder.exe --um ...`: run universal-modder's CLI from the checkout with the exe's bundled Python."""
    ensure(log=lambda m: print(m, file=sys.stderr))
    sys.path.insert(0, str(toolkit_dir()))
    from um.cli import main

    main(args)
