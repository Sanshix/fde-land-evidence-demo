"""Version the actual rate table. Changes require a transaction-bound impact preview."""

import hashlib
import json
import time
from datetime import date

from . import store
from .domain import dec


def validate(rates):
    if not isinstance(rates, list) or not rates or len(rates) > 1000:
        raise ValueError("计租标准必须为非空数组，上限1000条")
    out, keys = [], set()
    for row in rates:
        day = date.fromisoformat(row["执行起始日"]).isoformat()
        use = row["用途"]
        if use not in {"住宅", "商服", "工业", "仓储"} or (day, use) in keys:
            raise ValueError("用途无效或生效日/用途重复")
        keys.add((day, use))
        r = {"执行起始日": day, "用途": use}
        for grade in ("一级", "二级", "三级", "四级"):
            amount = dec(row[grade])
            if amount < 0 or amount > 1000000:
                raise ValueError("费率超出可接受范围")
            r[grade] = str(amount)
        out.append(r)
    return sorted(out, key=lambda r: (r["执行起始日"], r["用途"]))


def digest(rates):
    return hashlib.sha256(store.dumps(rates).encode()).hexdigest()


def initialize(source):
    with store.connection() as db:
        if not db.execute("SELECT value FROM settings WHERE key='active_ruleset'").fetchone():
            rates = validate(source["rates"])
            key = digest(rates)
            db.execute(
                "INSERT OR IGNORE INTO rulesets VALUES(?,?,?,?,?)",
                (key, store.dumps(rates), time.time(), "system", "原始练习集05计租标准；核算口径06-v1"),
            )
            db.execute("INSERT INTO settings VALUES('active_ruleset',?)", (key,))


def source_for(db, base):
    row = db.execute(
        "SELECT r.id,r.payload FROM rulesets r JOIN settings s ON s.value=r.id WHERE s.key='active_ruleset'"
    ).fetchone()
    return {**base, "rates": json.loads(row["payload"]), "rate_version": row["id"]} if row else base
