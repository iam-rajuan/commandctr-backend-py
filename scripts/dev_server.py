from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
from collections.abc import Sequence


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def build_command(host: str, port: int, reload: bool) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if reload:
        command.append("--reload")
    return command


def start_server(command: Sequence[str]) -> subprocess.Popen[bytes]:
    clear_screen()
    print("Starting FastAPI dev server")
    print("Type 'rs' + Enter to clear logs and restart. Type 'q' + Enter to stop.\n")
    return subprocess.Popen(command, stdin=subprocess.DEVNULL)


def stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the FastAPI dev server with nodemon-style rs restart.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--no-reload", action="store_true", help="Disable Uvicorn file watching.")
    args = parser.parse_args()

    command = build_command(args.host, args.port, reload=not args.no_reload)
    stop_event = threading.Event()
    restart_event = threading.Event()

    def read_commands() -> None:
        while not stop_event.is_set():
            try:
                value = input().strip().lower()
            except EOFError:
                stop_event.set()
                return
            if value == "rs":
                restart_event.set()
            elif value in {"q", "quit", "exit"}:
                stop_event.set()

    input_thread = threading.Thread(target=read_commands, daemon=True)
    input_thread.start()

    process = start_server(command)
    try:
        while not stop_event.is_set():
            if restart_event.is_set():
                restart_event.clear()
                stop_server(process)
                process = start_server(command)
            elif process.poll() is not None:
                return process.returncode or 0
            else:
                stop_event.wait(0.2)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        stop_server(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
