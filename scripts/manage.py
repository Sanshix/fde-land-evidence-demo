#!/usr/bin/env python3
"""Local operational commands; never create cloud accounts or generated credentials."""

import argparse
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import store
from app.maintenance import create_backup, restore_backup

parser = argparse.ArgumentParser(description="FDE local operations")
sub = parser.add_subparsers(dest="command", required=True)
sub.add_parser("migrate")
p = sub.add_parser("backup")
p.add_argument("destination", type=Path)
p = sub.add_parser("restore")
p.add_argument("backup", type=Path)
p.add_argument("destination", type=Path)
p = sub.add_parser("add-user")
p.add_argument("file", type=Path)
p.add_argument("name")
p.add_argument("--role", choices=["viewer", "reviewer", "admin"], required=True)
args = parser.parse_args()
if args.command == "migrate":
    store.init()
    print("schema migrated; no model calls")
elif args.command == "backup":
    result = create_backup(store.DATA, args.destination, store.DB)
    print(json.dumps({"path": str(args.destination), "files": len(result)}, ensure_ascii=False))
elif args.command == "restore":
    print(json.dumps(restore_backup(args.backup, args.destination), ensure_ascii=False, indent=2))
else:
    accounts = json.loads(args.file.read_text()) if args.file.exists() else {}
    if args.name in accounts:
        parser.error("用户已存在；禁止无确认覆盖")
    password = getpass.getpass("新用户密码（自行指定，不会显示）: ")
    confirm = getpass.getpass("再次输入: ")
    if password != confirm or len(password) < 12:
        parser.error("密码需一致且至少12字符")
    salt = os.urandom(16)
    accounts[args.name] = {
        "role": args.role,
        "salt": salt.hex(),
        "hash": hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex(),
    }
    args.file.parent.mkdir(parents=True, exist_ok=True)
    args.file.write_text(json.dumps(accounts, indent=2))
    args.file.chmod(0o600)
    print("本地账户已添加；请使用 FDE_USERS_FILE 显式启用。未创建任何外部凭据。")
