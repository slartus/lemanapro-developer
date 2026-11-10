from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from lpdev.main import DEFAULT_PORT, bootstrap, create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="lpdev")
    commands = parser.add_subparsers(dest="command", required=True)
    timesheet = commands.add_parser("timesheet", help="run only the timesheet page until Ctrl+C")
    timesheet.add_argument("--port", type=int, default=DEFAULT_PORT)
    timesheet.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    ctx, _ = bootstrap(timesheet_only=True)
    app = create_app(ctx, args.port, timesheet_only=True)
    if not args.no_browser:
        timer = threading.Timer(1.0, webbrowser.open, [f"http://127.0.0.1:{args.port}/timesheet"])
        timer.daemon = True
        timer.start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)
