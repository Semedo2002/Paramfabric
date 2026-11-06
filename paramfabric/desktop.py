"""Native desktop entry point backed by the local FastAPI Studio.

Double-clicking `ParamfabricStudio.exe` (or `Start Paramfabric.bat`)
starts a loopback-only server, issues a Local Admin session token, and opens
the workbench window with the token already injected: no sign-in prompt.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from paramfabric.server.app import create_app

WINDOW_TITLE = "Paramfabric Studio"


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def workspace_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


@dataclass
class LocalServer:
    """A running loopback Studio server"""

    app: FastAPI
    server: uvicorn.Server
    thread: threading.Thread
    base_url: str

    @property
    def studio_url(self) -> str:
        token = self.app.state.local_session_token
        return f"{self.base_url}/studio" + (f"#token={token}" if token else "")

    def wait(self) -> None:
        try:
            while self.thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def start_local_server(fabric_path: str | Path = "params.yaml", *, timeout: float = 30) -> LocalServer:
    """Start the API on a free loopback port and wait until it is healthy"""
    port = _available_port()
    enterprise_auth=os.getenv("PARAMFABRIC_AUTH_REQUIRED", "false").lower() == "true"
    if not enterprise_auth:
        local_state = Path.cwd() / ".paramfabric"
        local_state.mkdir(parents=True, exist_ok=True)
        local_database = (local_state / "studio.db").resolve().as_posix()
        os.environ.setdefault("PARAMFABRIC_DATABASE_URL", f"sqlite:///{local_database}")
        os.environ.setdefault("PARAMFABRIC_AUDIT_PATH", str(local_state / "audit-ledger.jsonl"))
    app = create_app(fabric_path, auth_required=enterprise_auth)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread=threading.Thread(target=server.run, name="paramfabric-api", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    print(f"Starting local Paramfabric server at {base_url}…", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not thread.is_alive():
            raise RuntimeError("Paramfabric server stopped before the Studio became available")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.2)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise TimeoutError(f"Paramfabric server did not start within {timeout:g} seconds")
    print("Local Paramfabric server is ready.", flush=True)
    return LocalServer(app=app, server=server, thread=thread, base_url=base_url)


def _open_in_browser(local: LocalServer) -> None:
    webbrowser.open(local.studio_url)
    local.wait()


def launch(fabric_path: str | Path = "params.yaml", *, open_window: bool = True) -> None:
    local = start_local_server(fabric_path)
    try:
        if not open_window:
            print(f"Studio: {local.studio_url}", flush=True)
            local.wait()
            return
        try:
            import webview
        except ImportError:
            _open_in_browser(local)
            return
        try:
            webview.create_window(
                WINDOW_TITLE,
                local.studio_url,
                width=1600,
                height=980,
                min_size=(1180, 720),
                background_color="#0a0e14",
            )
            webview.start()
        except Exception as exc:  # noqa: BLE001 - any GUI backend failure falls back to the browser
            print(f"Native webview could not start ({exc}); opening the local Studio in your browser.")
            _open_in_browser(local)
    finally:
        local.stop()


def main(argv: list[str] | None = None) -> None:
    root=workspace_root()
    if getattr(sys, "frozen", False):
        os.chdir(root)
    parser = argparse.ArgumentParser(description="Launch the Paramfabric desktop Studio.")
    parser.add_argument("--fabric", type=Path, default=root / "params.yaml")
    parser.add_argument("--no-window", action="store_true", help="serve only and print the Studio URL")
    arguments = parser.parse_args(argv)
    launch(arguments.fabric, open_window=not arguments.no_window)


if __name__ == "__main__":
    main()
