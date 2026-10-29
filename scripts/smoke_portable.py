"""Smoke test frozen application without using the build interpreter's imports."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path


def smoke(build_dir):
    (archive,) = Path(build_dir).glob("fde-*.zip")
    with tempfile.TemporaryDirectory(prefix="fde portable 中文 ") as temp:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(temp)
        (root,) = Path(temp).glob("fde-*")
        manifest = json.loads((root / "BUILD_MANIFEST.json").read_text())
        import hashlib

        for name, digest in manifest["files"].items():
            assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
        assert not (root / "data").exists()
        assert not (root / ".env").exists()
        executable = root / ("fde.exe" if os.name == "nt" else "fde")
        if os.name != "nt":
            executable.chmod(0o755)
            for file in root.rglob("*.so*"):
                file.chmod(0o755)
        subprocess.run([str(executable), "--install"], check=True, cwd=temp)
        subprocess.run([str(executable), "--install"], check=True, cwd=temp)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {**os.environ, "FDE_PORT": str(port), "FDE_HOST": "127.0.0.1"}
        process = subprocess.Popen([str(executable), "--no-browser"], cwd=temp, env=env)
        try:
            for _ in range(90):
                if process.poll() is not None:
                    raise RuntimeError(f"Startup failed: {process.returncode}")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                        assert response.status == 200
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError("Startup timeout")
            for path in ("/", "/static/app.js"):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
                    assert response.status == 200
            assert (root / "data/fde.sqlite").is_file()
            print("PASS: extracted path with spaces/Chinese, repeat installation, HTTP, UI, database")
        finally:
            process.terminate()
            process.wait(timeout=15)


if __name__ == "__main__":
    smoke(sys.argv[1])
