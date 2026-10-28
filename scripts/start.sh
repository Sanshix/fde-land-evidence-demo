#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
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
    # 供多名经办人员在内网访问：必须启用本地账号，并声明浏览器访问地址（防跨站写入）
    if [ -z "${FDE_USERS_FILE:-}" ] || [ -z "${FDE_ALLOWED_ORIGINS:-}" ]; then
      echo "监听 $FDE_HOST 需同时设置 FDE_USERS_FILE（本地账号）和 FDE_ALLOWED_ORIGINS（如 http://10.1.2.3:$FDE_PORT）" >&2
      exit 1
    fi
    ;;
esac
exec .venv/bin/python -m uvicorn app.main:app --host "$FDE_HOST" --port "$FDE_PORT" --workers 1
