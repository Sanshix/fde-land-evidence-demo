#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
# 当前演示数据；允许调用者显式覆盖。
export FDE_HOST=${FDE_HOST:-0.0.0.0}
export FDE_PORT=${FDE_PORT:-8765}
# 当前局域网演示按要求免登录；账号文件保留，方便后续恢复。
unset FDE_USERS_FILE
export FDE_ALLOW_UNAUTHENTICATED_LAN=1
export FDE_ALLOWED_ORIGINS=${FDE_ALLOWED_ORIGINS:-http://192.168.1.16:$FDE_PORT}
# 数据目录和基础表由 start.sh 读取本机 config/workbench.env。
# 不再默认绑定某次测试目录，也不默认禁用新上传材料的识别任务。
exec sh scripts/start.sh
