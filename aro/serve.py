"""aro serve — view a run's decision tree over HTTP (for a headless server run).

When ARO runs unattended on a server, the report (`decision-tree.html`) lives in a
file you can't open in a remote browser. This serves a run's out-dir over HTTP so the
report is reachable on a port (default 8010), and — unless `--no-watch` — re-renders
the HTML from the live `events.jsonl` every few seconds, so the page reflects the run's
progress while it's still going (a poor-man's live dashboard).

    python3 -m aro serve <out-dir> [--port 8010] [--every 30] [--no-watch] [--host H]

Pure stdlib (http.server + threading), zero deps. Binds 127.0.0.1 by default; pass
--host 0.0.0.0 explicitly to be
reachable across the network — put it behind a firewall or an SSH tunnel; it serves
whatever is in the directory and is NOT authenticated.
"""
from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _rerender(out_dir: Path) -> bool:
    """Rebuild decision-tree.html from the out-dir's events.jsonl. Best-effort — a
    mid-write events log or an empty run just leaves the previous HTML in place."""
    try:
        from . import tree as treemod
        t = treemod.build_tree(out_dir)
        (out_dir / "decision-tree.html").write_text(treemod.render_html(t, t["spec"]))
        return True
    except Exception:
        return False


def _watch(out_dir: Path, every: int, stop: threading.Event) -> None:
    while not stop.is_set():
        _rerender(out_dir)
        stop.wait(every)


class _Handler(SimpleHTTPRequestHandler):
    def do_GET(self):                       # `/` → the report, not a dir listing
        if self.path in ("/", ""):
            self.path = "/decision-tree.html"
        return super().do_GET()

    def end_headers(self):                  # never cache — the file changes under the browser
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *a):              # quiet (a run is long; don't spam the console)
        pass


def cli(args) -> None:
    out_dir = Path(args.out_dir).expanduser().resolve()
    if not out_dir.is_dir():
        raise SystemExit(f"serve: {out_dir} is not a directory (point it at a run's --out-dir)")
    port = args.port
    every = max(5, args.every)
    host = args.host
    watch = args.watch

    if not (out_dir / "decision-tree.html").exists():
        _rerender(out_dir)                  # render once so there's something to serve now

    stop = threading.Event()
    if watch:
        threading.Thread(target=_watch, args=(out_dir, every, stop), daemon=True).start()

    httpd = ThreadingHTTPServer((host, port), partial(_Handler, directory=str(out_dir)))
    live = f" · live re-render every {every}s" if watch else " (static; pass without --no-watch for live)"
    print(f"aro serve: {out_dir}")
    print(f"  → http://{host}:{port}/   (decision-tree.html{live})")
    print("  Ctrl-C to stop." + ("" if host != "0.0.0.0" else
          "  NOTE: 0.0.0.0 is network-reachable + unauthenticated — firewall it or SSH-tunnel."))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        stop.set()
        httpd.shutdown()
