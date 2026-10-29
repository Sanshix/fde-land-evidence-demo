#!/usr/bin/env python3
"""Build native self-contained release; run once on Windows and once on Linux."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    system = platform.system().lower()
    if system not in ("windows", "linux") or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SystemExit("仅在 Windows x64 或 Linux x86_64 构建；不可从 macOS 交叉编译。")
    target = output.resolve()
    if target.exists():
        raise SystemExit("输出目录已存在，请选择新目录。")
    safe_config = target / "safe-config"
    shutil.copytree(
        ROOT / "config",
        safe_config,
        ignore=shutil.ignore_patterns("providers.json", "users.json", "notice.json", "__pycache__"),
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--contents-directory",
            ".",
            "--name",
            "fde",
            "--distpath",
            str(target / "dist"),
            "--workpath",
            str(target / "work"),
            "--specpath",
            str(target),
            "--paths",
            str(ROOT),
            "--collect-all",
            "pypdfium2",
            "--collect-all",
            "uvicorn",
            "--collect-submodules",
            "app",
            "--add-data",
            f"{ROOT / 'app/static'}{':'}app/static",
            "--add-data",
            f"{safe_config}{':'}config",
            str(ROOT / "packaging/launcher.py"),
        ],
        check=True,
        cwd=ROOT,
    )
    release = target / f"fde-{system}-x64"
    shutil.move(str(target / "dist/fde"), release)
    # Private overrides are never shipped, including when present on a developer machine.
    for name in ("providers.json", "users.json"):
        (release / "config" / name).unlink(missing_ok=True)
    samples = ROOT / "packaging/business-data"
    destination = release / "练习集"
    destination.mkdir()
    for prefix in ("02_", "03_", "04_", "05_", "06_"):
        for file in samples.glob(prefix + "*.xlsx"):
            shutil.copy2(file, destination / file.name)
    shutil.copytree(samples / "07_模板", destination / "07_模板")
    (destination / "01_合同扫描件").mkdir()
    source = release / "源代码"
    for folder in ("app", "config", "scripts", "packaging"):
        shutil.copytree(
            ROOT / folder,
            source / folder,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "providers.json", "users.json", "notice.json"
            ),
        )
    for name in ("pyproject.toml", "requirements.in", ".env.example", "README.md"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copy2(ROOT / "docs/一键离线部署.md", release / "部署说明.md")
    if system == "windows":
        for name, argument in (("安装", "--install"), ("启动", "")):
            (release / f"{name}.cmd").write_bytes(
                (
                    '@echo off\r\nchcp 65001 >nul\r\ncd /d "%~dp0"\r\n'
                    f'"%~dp0fde.exe" {argument}\r\n'
                    "if errorlevel 1 (echo 执行失败，请查看上方错误信息。 & pause & exit /b 1)\r\n"
                    + ("pause\r\n" if argument else "")
                ).encode("utf-8")
            )
    else:
        for name, argument in (("安装", "--install"), ("启动", "")):
            path = release / f"{name}.sh"
            path.write_text(
                '#!/bin/sh\nset -eu\ncd "$(dirname "$0")"\nchmod +x ./fde\nexec ./fde ' + argument + ' "$@"\n'
            )
            path.chmod(0o755)
    executable = release / ("fde.exe" if system == "windows" else "fde")
    subprocess.run([str(executable), "--install"], cwd=release, check=True)
    # Installation check must not seed the delivered package with a developer database.
    shutil.rmtree(release / "data")
    manifest = {
        "system": system,
        "architecture": "x64",
        "python": sys.version,
        "runtime_included": True,
        "cloud_models_require_network": True,
        "files": {
            str(f.relative_to(release)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(release.rglob("*"))
            if f.is_file()
        },
    }
    (release / "BUILD_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    archive = shutil.make_archive(str(target / release.name), "zip", target, release.name)
    print(archive)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    build(parser.parse_args().output)
