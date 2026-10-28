#!/usr/bin/env python3
"""Build on an authorized online machine. Installer later uses only local wheel files.
Requires a Python with pip. No model downloads; never packages secrets, DBs or answers.
"""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument("--output", type=Path, required=True)
p.add_argument("--cached-wheels", type=Path)
a = p.parse_args()
out = a.output.resolve()
if out.exists():
    p.error("output must not exist")
(out / "demo").mkdir(parents=True)
demo = out / "demo"
for name in ("app", "config"):
    shutil.copytree(
        ROOT / name, demo / name, ignore=shutil.ignore_patterns("__pycache__", "providers.json", "users.json")
    )
(demo / "scripts").mkdir()
for name in ("manage.py", "install.sh", "start.sh"):
    shutil.copy2(ROOT / "scripts" / name, demo / "scripts" / name)
for name in ("pyproject.toml", "README.md", "requirements.in"):
    shutil.copy2(ROOT / name, demo / name)
samples = out / "练习集"
samples.mkdir()
for f in (ROOT.parent / "练习集").iterdir():
    if f.is_file() and f.name.startswith(("02_", "03_", "04_", "05_", "06_")):
        shutil.copy2(f, samples / f.name)
shutil.copytree(ROOT.parent / "练习集/07_模板", samples / "07_模板")
images = samples / "01_合同扫描件"
images.mkdir()
for prefix in (
    "LX-001",
    "LX-004",
    "LX-014",
    "LX-017",
    "LX-021",
    "LX-045",
    "LX-034",
    "LX-008",
    "LX-018",
    "LX-025",
):
    for f in (ROOT.parent / "练习集/01_合同扫描件").glob(prefix + "_*.jpg"):
        shutil.copy2(f, images / f.name)
wheelhouse = demo / "wheelhouse"
if a.cached_wheels:
    from cache_wheels import repack

    provenance = repack(a.cached_wheels, wheelhouse, ROOT / "requirements.in")
else:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "download",
            "--index-url",
            "https://pypi.org/simple/",
            "--cache-dir",
            "/tmp/fde-pip-cache",
            "--only-binary=:all:",
            "--no-deps",
            "-r",
            str(ROOT / "requirements.in"),
            "--dest",
            str(wheelhouse),
        ],
        check=True,
    )
    provenance = "official PyPI download"

versions = {}
for line in (ROOT / "requirements.in").read_text().splitlines():
    if line and not line.startswith("#"):
        name, version = line.split("==")
        versions[name.replace("-", "_").lower()] = (name, version)
locks = []
for wheel in sorted(wheelhouse.glob("*.whl")):
    name = wheel.name.split("-")[0].lower()
    package, version = versions[name]
    locks.append(f"{package}=={version} --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}")
(demo / "requirements.lock").write_text("\n".join(locks) + "\n")
manifest = {
    "system": platform.platform(),
    "machine": platform.machine(),
    "python": sys.version.split()[0],
    "runtime_included": False,
    "requires": "Python3.12+venv; macOS target only if built on macOS",
    "model_weights_included": False,
    "dependency_provenance": provenance,
    "cloud_inference_requires_network_and_authorization": True,
    "samples": "10 declared development documents only; no standard answers",
    "files": {
        str(f.relative_to(out)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in out.rglob("*")
        if f.is_file()
    },
}
(out / "BUILD_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
print(json.dumps({"output": str(out), "wheels": len(locks), "runtime_included": False}, ensure_ascii=False))
