"""Portable entry point. All writable state stays beside the executable."""

import argparse
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def bind_listener(host, port, allow_fallback):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind((host, port))
        except OSError:
            if not allow_fallback:
                raise
            listener.bind((host, 0))
        listener.listen(128)
        return listener
    except BaseException:
        listener.close()
        raise


def main():
    root = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[1]
    )
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ.setdefault("FDE_DATA_DIR", str(root / "data"))
    os.environ.setdefault("FDE_SAMPLES_DIR", str(root / "练习集"))
    parser = argparse.ArgumentParser(description="土地租赁管理：离线安装与启动")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--auto-port", action="store_true", help="本机端口冲突时自动切换，包括环境变量指定的端口"
    )
    args = parser.parse_args()
    from app import store

    store.init()
    if args.install:
        print("安装完成。数据保存在 data 目录；运行启动脚本即可使用。")
        return
    host = os.environ.get("FDE_HOST", "127.0.0.1")
    port = int(os.environ.get("FDE_PORT", "18765"))
    if host not in ("127.0.0.1", "localhost"):
        if not os.environ.get("FDE_ALLOWED_ORIGINS") or not os.environ.get("FDE_USERS_FILE"):
            raise SystemExit("远程访问需配置 FDE_ALLOWED_ORIGINS 和 FDE_USERS_FILE")
    # Keep the socket reserved until handed to the server, avoiding a port-selection race.
    listener = bind_listener(
        host, port, ("FDE_PORT" not in os.environ or args.auto_port) and host in ("127.0.0.1", "localhost")
    )
    selected_port = listener.getsockname()[1]
    if selected_port != port:
        print(f"默认端口 {port} 不可用，已自动切换到 {selected_port}。", flush=True)
    port = selected_port
    os.environ["FDE_PORT"] = str(port)
    url = f"http://127.0.0.1:{port}"
    if not args.no_browser and host in ("127.0.0.1", "localhost"):

        def open_when_ready():
            for _ in range(60):
                try:
                    with urllib.request.urlopen(url + "/healthz", timeout=1) as response:
                        if response.status == 200:
                            webbrowser.open(url)
                            return
                except OSError:
                    time.sleep(0.5)

        threading.Thread(target=open_when_ready, daemon=True).start()
    import uvicorn

    from app.main import app

    print(f"服务地址：{url}，按 Ctrl+C 停止。", flush=True)
    with listener:
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, workers=1))
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
