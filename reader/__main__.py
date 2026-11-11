import argparse
import hashlib
import socket
import threading
import time
import webbrowser

import httpx
import uvicorn

from . import DATA, SHARED_BUILD, VERSION
from .app import create_app


def open_when_ready(server, address):
    deadline = time.monotonic() + 15
    while not server.should_exit and time.monotonic() < deadline:
        if server.started:
            webbrowser.open(address)
            return
        time.sleep(.1)


def shared_port(first=8767):
    """Reuse only this user's library; never open a different running library."""
    identity = hashlib.sha256(str(DATA).encode()).hexdigest()
    for port in range(first, first + 20):
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=.4, trust_env=False)
            info = response.json() if response.status_code == 200 else {}
            if info.get("app") == "paper-reader" and info.get("version") == VERSION and info.get("library_id") == identity:
                return port
        except (httpx.HTTPError, ValueError):
            pass
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    raise RuntimeError("阅读器端口已被占用，请关闭不再使用的阅读器后重试。")


def main():
    parser = argparse.ArgumentParser(description="本地文献阅读器")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    port = args.port if args.port is not None else (shared_port() if SHARED_BUILD else 8767)
    address = f"http://127.0.0.1:{port}"
    try:
        response = httpx.get(address + "/api/health", timeout=1, trust_env=False)
        if response.status_code == 200 and response.json().get("app") == "paper-reader":
            if SHARED_BUILD and response.json().get("library_id") != hashlib.sha256(str(DATA).encode()).hexdigest():
                raise RuntimeError("此端口正在运行另一个文献库，请关闭该阅读器或使用其他端口。")
            if response.json().get("version") != VERSION:
                raise RuntimeError("旧版文献阅读器仍在运行。请先从旧界面的设置中退出阅读器，再打开快捷方式。")
            if not args.no_browser:
                webbrowser.open(address)
            print("文献阅读器已在运行：" + address)
            return
    except (httpx.HTTPError, ValueError):
        pass
    app = create_app(stop_server=lambda: setattr(server, "should_exit", True))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=8))
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(server, address), daemon=True).start()
    print("文献阅读器：" + address)
    server.run()


if __name__ == "__main__":
    main()
