"""Read only explicitly authorized project variables. Never source shell, discover accounts or log values."""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_KEYS = {
    "AI_GUIJILIUDONG_SECRET",
    "AI_ALIYUN_SECRET",
    "AI_ALIYUN_KEY",
    "AI_BAIDU_APIID",
    "AI_BAIDU_KEY",
    "AI_BAIDU_SECRET",
    "AI_BAIDU_QIANFAN_API_KEY",
}
BINDINGS = {
    "AI_GUIJILIUDONG_SECRET": "api.siliconflow.cn",
    "AI_ALIYUN_SECRET": "dashscope.aliyuncs.com",
    "AI_ALIYUN_KEY": ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"),
    "AI_BAIDU_QIANFAN_API_KEY": "qianfan.baidubce.com",
    "AI_BAIDU_KEY": "aip.baidubce.com",
    "AI_BAIDU_SECRET": "aip.baidubce.com",
}


def read_project_values():
    out = {}
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)$", line)
            if not match or match[1] not in PROJECT_KEYS:
                continue
            value = match[2].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            out[match[1]] = value
    return out


def credential(name, host):
    if name in BINDINGS:
        allowed = BINDINGS[name]
        if host not in ((allowed,) if isinstance(allowed, str) else allowed):
            raise ValueError("项目凭据与已批准目标域名不匹配")
        value = os.environ.get(name) or read_project_values().get(name)
    elif name.startswith("FDE_"):
        value = os.environ.get(name)
    else:
        raise ValueError("不是已批准的项目凭据变量")
    if not value:
        raise ValueError("所选提供方的凭据未配置")
    return value
