"""The desktop planner app: a local JSON API (`server`), background jobs (`jobs`), and one native window.

`main()` starts the server on a thread and opens a pywebview window at it; closing the window stops the server
and ends the process. pywebview is imported only there, so `import rune_advisor.cli` never needs it.
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rune-advisor", description="Rune Advisor planner window")
    parser.add_argument("--no-window", action="store_true",
                        help="serve only and print the URL with its secret (development, tests)")
    parser.add_argument("--port", type=int, default=0, help="port for --no-window (default: random)")
    args = parser.parse_args(argv)
    return run(no_window=args.no_window, port=args.port)


def run(no_window: bool = False, port: int = 0) -> int:
    from .server import AppServer

    server = AppServer(port=port if no_window else 0)
    if no_window:
        print(f"serving {server.start_url}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    return _window(server)


def _window(server) -> int:
    """Thin shell, not exercised in the headless sandbox: serve on a thread, block on the window, then stop."""
    import threading

    try:
        import webview
    except ImportError:
        print("The planner window needs pywebview: install rune-advisor[ui] (or run with --no-window).")
        server.server_close()
        return 1
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        webview.create_window("Rune Advisor", server.start_url, width=1280, height=860, min_size=(900, 600))
        webview.start()  # returns when the window is closed
    finally:
        server.shutdown()
        server.server_close()
    return 0
