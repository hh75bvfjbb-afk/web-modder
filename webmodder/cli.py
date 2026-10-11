"""`python -m webmodder` / `python run.py`: start the local server."""
from __future__ import annotations

import argparse
import os
import sys

from webmodder import __version__


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):  # Russian text must not crash a cp1252 console or a redirected log
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    if argv[:1] == ["--um"]:  # the exe doubles as the `um` command for the agent
        from webmodder.toolkit import run_um

        return run_um(argv[1:])
    ap = argparse.ArgumentParser(prog="webmodder", description="Web Modder: моды для любых игр через браузер")
    ap.add_argument("--host", default=os.environ.get("WM_HOST", "127.0.0.1"),
                    help="адрес (по умолчанию 127.0.0.1 — только этот ПК)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WM_PORT", "8765")))
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    ap.add_argument("--add-key", action="append", default=[], metavar="KEY", help="добавить ключ перед запуском")
    ap.add_argument("--version", action="version", version=f"web-modder {__version__}")
    a = ap.parse_args(argv)
    if a.add_key:
        from webmodder.keys import KeyPool

        pool = KeyPool()
        for k in a.add_key:
            try:
                r = pool.add(k)
                print(f"ключ добавлен: {r['label']} · {r['model']} · {r['status']}")
            except ValueError as e:
                print(f"ключ не добавлен: {e}")
    from webmodder.server import serve

    serve(a.host, a.port, open_browser=not a.no_browser)


if __name__ == "__main__":
    main()
