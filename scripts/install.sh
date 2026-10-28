#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
FDE_PYTHON=${FDE_PYTHON:-python3.12}
"$FDE_PYTHON" -c 'import sys; assert sys.version_info[:2] == (3,12), "Requires Python 3.12"'
if [ ! -d wheelhouse ]; then
  echo '缺少离线 wheelhouse；在联网构建机运行 scripts/build_offline.py，不在安装时下载。' >&2
  exit 1
fi
"$FDE_PYTHON" -m venv .venv
.venv/bin/python -m pip install --no-index --find-links=wheelhouse --require-hashes -r requirements.lock
.venv/bin/python scripts/manage.py migrate
printf '安装完成。运行 ./scripts/start.sh；服务仅监听 127.0.0.1:8765。\n'
