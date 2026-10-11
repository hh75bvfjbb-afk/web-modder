"""Build WebModder.exe (Windows) or WebModder (Linux/macOS) as one file with PyInstaller.

    pip install -r requirements.txt pyinstaller
    python packaging/build_exe.py            # -> dist/WebModder.exe

The exe carries Python, the app, the libraries universal-modder's `um` CLI needs (Pillow, numpy, PyYAML) and a
snapshot of universal-modder itself, so it runs on a PC with nothing installed. `um` runs as `WebModder.exe --um`.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
REPO = os.environ.get("WM_TOOLKIT_REPO", "https://github.com/rehan-remade/universal-modder")
NOT_BUNDLED = {"bpy", "mathutils", "__future__"}  # bpy/mathutils run inside Blender, not in the exe


def toolkit_snapshot() -> Path:
    dst = BUILD / "universal-modder"
    shutil.rmtree(dst, ignore_errors=True)
    BUILD.mkdir(exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1", REPO, str(dst)], check=True)
    shutil.rmtree(dst / ".git", ignore_errors=True)
    return dst


def um_imports(tk: Path) -> list[str]:
    """Everything um/*.py imports: the exe must contain it even though our own code never imports um."""
    mods = set()
    for f in (tk / "um").rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                mods |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                mods.add(n.module)
    out = []
    for m in sorted(mods):
        if m.split(".")[0] in NOT_BUNDLED or m.startswith("um"):
            continue
        try:
            __import__(m)
            out.append(m)
        except ImportError:  # winreg on Linux and the like
            print(f"  (пропускаю {m}: нет на этой платформе)")
    return out


def main():
    import PyInstaller.__main__

    for stream in (sys.stdout, sys.stderr):  # a Windows CI console is cp1252: Cyrillic must not crash the build
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    tk = toolkit_snapshot()
    hidden = um_imports(tk) + ["numpy", "yaml", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "PIL.ImageFilter",
                                "PIL.ImageOps", "PIL.ImageChops", "PIL.ImageEnhance", "anthropic"]
    sep = os.pathsep
    args = [str(ROOT / "run.py"), "--onefile", "--name", "WebModder", "--console", "--noconfirm",
            "--distpath", str(ROOT / "dist"), "--workpath", str(BUILD / "pyinstaller"), "--specpath", str(BUILD),
            "--add-data", f"{ROOT / 'webmodder' / 'static'}{sep}webmodder/static",
            "--add-data", f"{tk}{sep}universal-modder",
            "--collect-submodules", "anthropic", "--collect-submodules", "PIL",
            "--exclude-module", "tkinter", "--exclude-module", "pytest"]
    for h in hidden:
        args += ["--hidden-import", h]
    PyInstaller.__main__.run(args)
    exe = ROOT / "dist" / ("WebModder.exe" if os.name == "nt" else "WebModder")
    print(f"\nготово: {exe} ({exe.stat().st_size / 1e6:.0f} МБ)")


if __name__ == "__main__":
    sys.exit(main())
