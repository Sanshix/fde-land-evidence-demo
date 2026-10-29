#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
# 本机工作台固定数据集；测试运行仍可通过显式环境变量隔离。
if [ -f config/workbench.env ]; then
  . ./config/workbench.env
fi
if [ ! -x .venv/bin/python ]; then
  echo '先运行 scripts/install.sh' >&2
  exit 1
fi
FDE_HOST=${FDE_HOST:-127.0.0.1}
FDE_PORT=${FDE_PORT:-8765}
export FDE_PORT
case "$FDE_HOST" in
  127.0.0.1|localhost) ;;
  *)
    # 默认要求账号；局域网演示可显式启用免登录，仍校验浏览器来源。
    if [ -z "${FDE_ALLOWED_ORIGINS:-}" ] || { [ -z "${FDE_USERS_FILE:-}" ] && [ "${FDE_ALLOW_UNAUTHENTICATED_LAN:-}" != 1 ]; }; then
      echo "监听 $FDE_HOST 需设置 FDE_ALLOWED_ORIGINS，并配置 FDE_USERS_FILE 或显式设置 FDE_ALLOW_UNAUTHENTICATED_LAN=1" >&2
      exit 1
    fi
    ;;
esac
exec .venv/bin/python -m uvicorn app.main:app --host "$FDE_HOST" --port "$FDE_PORT" --workers 1
