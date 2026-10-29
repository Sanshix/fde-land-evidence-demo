#!/usr/bin/env python3
"""Assemble a Windows x64 offline bundle on any host; no cross compilation.

python scripts/build_windows_bundle.py --output /absolute/new/directory
Needs pip and network only on the build machine. The target machine needs neither.
"""

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
PRIVATE = ("__pycache__", "*.pyc", ".DS_Store", "providers.json", "users.json", "notice.json", ".env")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract(archive, destination):
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            path = (destination / name).resolve()
            if not path.is_relative_to(destination.resolve()):
                raise ValueError(f"Unsafe archive member: {name}")
        bundle.extractall(destination)


def pe_x64(path):
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise ValueError(f"Not Windows PE: {path}")
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset : offset + 4] != b"PE\0\0" or struct.unpack_from("<H", data, offset + 4)[0] != 0x8664:
        raise ValueError(f"Not Windows x64: {path}")


def build(output, runtime_zip=None, wheels=None):
    output = output.resolve()
    if output.exists():
        raise SystemExit("Output directory already exists; choose a new directory.")
    output.mkdir(parents=True)
    release = output / "FDE-Windows-x64"
    release.mkdir()
    with tempfile.TemporaryDirectory(prefix="fde-win-build-") as temp:
        temp = Path(temp)
        if runtime_zip is None:
            runtime_zip = temp / "python-embed.zip"
            urllib.request.urlretrieve(PYTHON_URL, runtime_zip)
        if wheels is None:
            wheels = temp / "wheels"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "--isolated",
                    "download",
                    "--index-url",
                    "https://pypi.org/simple",
                    "--only-binary=:all:",
                    "--platform",
                    "win_amd64",
                    "--python-version",
                    "3.12",
                    "--implementation",
                    "cp",
                    "--abi",
                    "cp312",
                    "-r",
                    str(ROOT / "requirements.in"),
                    "colorama==0.4.6",
                    "--dest",
                    str(wheels),
                ],
                check=True,
            )
        runtime = release / "runtime"
        runtime.mkdir()
        extract(runtime_zip, runtime)
        # _pth isolates this interpreter from user Python, PATH and user site packages.
        (runtime / "python312._pth").write_text(
            "python312.zip\n.\nLib/site-packages\n..\nimport site\n", encoding="utf-8"
        )
        vendor = runtime / "Lib/site-packages"
        vendor.mkdir(parents=True)
        inputs = {"python_url": PYTHON_URL, "python_sha256": sha(runtime_zip), "wheels": {}}
        for wheel in sorted(wheels.glob("*.whl")):
            if not wheel.name.endswith(("-any.whl", "-win_amd64.whl")):
                raise ValueError(f"Wrong platform wheel: {wheel.name}")
            extract(wheel, vendor)
            inputs["wheels"][wheel.name] = sha(wheel)
        if not inputs["wheels"]:
            raise ValueError("Wheel directory is empty")
        # All shipped native binaries must target Windows AMD64.
        binaries = list(runtime.rglob("*.exe")) + list(runtime.rglob("*.dll")) + list(runtime.rglob("*.pyd"))
        for binary in binaries:
            pe_x64(binary)
        for folder in ("app", "config", "scripts", "packaging"):
            shutil.copytree(ROOT / folder, release / folder, ignore=shutil.ignore_patterns(*PRIVATE))
        (release / "docs").mkdir()
        for name in ("pyproject.toml", "requirements.in", ".env.example", "README.md"):
            shutil.copy2(ROOT / name, release / name)
        shutil.copy2(ROOT / "docs/一键离线部署.md", release / "docs/一键离线部署.md")
        shutil.copytree(ROOT / "packaging/business-data", release / "练习集")
        (release / "练习集/01_合同扫描件").mkdir(exist_ok=True)
        # A public provider template, never the developer's active configuration.
        shutil.copy2(release / "config/providers.competition.json", release / "config/providers.json")
        for name, argument in (("安装", "--install"), ("启动", ""), ("install", "--install"), ("start", "")):
            command = (
                '@echo off\r\nchcp 65001 >nul\r\nsetlocal\r\ncd /d "%~dp0"\r\n'
                'if not exist "runtime\\python.exe" (echo 请先完整解压 ZIP，再运行此脚本。 & pause & exit /b 1)\r\n'
                f'"%~dp0runtime\\python.exe" -X utf8 "%~dp0packaging\\launcher.py" {argument}\r\n'
                "if errorlevel 1 (echo 运行失败，请保留本窗口错误信息。 & pause & exit /b 1)\r\n"
                + ("echo 安装完成，请双击启动.cmd。\r\npause\r\n" if argument else "")
            )
            (release / (name + ".cmd")).write_bytes(command.encode("utf-8"))
        # A single entry point checks, repairs offline if needed, and then starts.
        bootstrap = (ROOT / "packaging/deploy-run.cmd").read_bytes()
        for name in ("部署并运行.cmd", "一键启动.cmd", "启动.cmd", "start.cmd"):
            (release / name).write_bytes(bootstrap)
        offline = release / "offline"
        offline.mkdir()
        application_files = {
            p.relative_to(release).as_posix(): sha(p)
            for p in sorted((release / "app").rglob("*"))
            if p.is_file()
        }
        application_files["packaging/launcher.py"] = sha(release / "packaging/launcher.py")
        (offline / "application-files.json").write_text(
            json.dumps(
                {
                    "files": application_files,
                    "required_files": [
                        p.relative_to(release).as_posix()
                        for folder in ("config", "练习集")
                        for p in sorted((release / folder).rglob("*"))
                        if p.is_file()
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        with zipfile.ZipFile(offline / "deployment.zip", "w", zipfile.ZIP_DEFLATED) as payload:
            for folder in ("runtime", "app", "config", "packaging", "scripts", "练习集"):
                for file in sorted((release / folder).rglob("*")):
                    if file.is_file():
                        payload.write(file, file.relative_to(release).as_posix())
        (offline / "deployment.sha256").write_text(sha(offline / "deployment.zip"), encoding="ascii")
        (release / "使用说明.txt").write_text(
            "Windows x64 一键部署运行包\n\n"
            "1. 将整个 ZIP 完整解压到可写目录（不要在压缩包内直接运行）。\n"
            "2. 双击 部署并运行.cmd：自动检查环境，完整则直接启动，缺失则从包内offline目录恢复后启动。\n"
            "3. 浏览器访问 http://127.0.0.1:18765；启动后会尝试自动打开。\n"
            "4. 服务窗口保持打开；Ctrl+C 停止。退出后窗口保留，运行日志在 logs 目录。\n"
            "5. 无需联网安装、Python、Docker或管理员权限；需要系统自带 Windows PowerShell 5.1。\n\n"
            "系统目标：Windows 10/11、Windows Server 2019/2022，x64。\n"
            "默认端口18765占用时会自动选择空闲端口，以启动窗口实际地址为准。\n显式指定端口：在命令提示符执行 set FDE_PORT=18766，再执行 start.cmd。\n"
            "全部源码位于 app/config/scripts/packaging；runtime 是内置 Python 与依赖。\n"
            "data 保存业务数据库及材料，重复安装不会清空；备份前停止服务并复制整个目录。\n"
            "默认仅限本机访问；远程访问须配置账号与允许来源，详见 docs 中部署说明。\n\n"
            "附带练习集基础表和模板，不含扫描合同、标准答案或开发机数据。\n"
            "安装和业务管理可离线；云识别需联网和密钥。在根目录创建 .env，参考 .env.example。\n"
            "默认供应商配置来自公开的 providers.competition.json，未附任何密钥。\n"
            "包已完成文件、依赖及原生二进制平台检查；Windows 原生运行验收尚未完成。\n",
            encoding="utf-8-sig",
        )
        manifest = {
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
            "target": "Windows x64",
            "runtime_included": True,
            "runtime": "CPython 3.12.10 embedded",
            "launcher_version": "20261029.3",
            "entrypoint": "部署并运行.cmd",
            "offline_repair_included": True,
            "inputs": inputs,
            "native_pe_x64_files_checked": len(binaries),
            "windows_execution_verified": False,
            "files": {
                p.relative_to(release).as_posix(): sha(p) for p in sorted(release.rglob("*")) if p.is_file()
            },
        }
        (release / "BUILD_MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    archive = Path(shutil.make_archive(str(output / release.name), "zip", output, release.name))
    (output / (archive.name + ".sha256")).write_text(f"{sha(archive)}  {archive.name}\n", encoding="ascii")
    print(
        json.dumps(
            {
                "zip": str(archive),
                "size_bytes": archive.stat().st_size,
                "native_binaries_checked": len(binaries),
            },
            ensure_ascii=False,
        )
    )
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-zip", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    args = parser.parse_args()
    build(args.output, args.runtime_zip, args.wheelhouse)
