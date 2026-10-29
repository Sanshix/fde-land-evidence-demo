"""Snapshot and restore to a NEW directory; credentials are intentionally excluded."""

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_backup(data_dir, destination, database=None):
    data_dir, destination = Path(data_dir), Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    database = Path(database) if database else data_dir / "fde.sqlite"
    source = sqlite3.connect(database)
    target = sqlite3.connect(destination / "fde.sqlite")
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    for name in ("objects", "runs"):
        if (data_dir / name).exists():
            shutil.copytree(data_dir / name, destination / name)
    files = {str(p.relative_to(destination)): sha(p) for p in destination.rglob("*") if p.is_file()}
    manifest = {
        "format": 1,
        "source_root": str(data_dir.resolve()),
        "source_roots": list(dict.fromkeys([str(data_dir.absolute()), str(data_dir.resolve())])),
        "files": files,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return files


def restore_backup(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError("恢复目标必须是不存在的新目录，禁止覆盖当前数据库")
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest.get("format") != 1:
        raise ValueError("未知备份格式")
    for name, expected in manifest["files"].items():
        path = source / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(source)
            or not path.is_file()
            or sha(path) != expected
        ):
            raise ValueError("备份文件缺失、越界或校验不一致：" + name)
    if "fde.sqlite" not in manifest["files"]:
        raise ValueError("备份缺少数据库")
    destination.mkdir(parents=True)
    incomplete = destination / "RESTORE_INCOMPLETE"
    incomplete.write_text("恢复尚未通过完整性与对象引用校验；禁止作为运行数据目录。")
    for name in manifest["files"]:
        out = destination / name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, out)
    db = sqlite3.connect(destination / "fde.sqlite")
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("数据库完整性校验失败")
        old_root = Path(manifest["source_root"])
        old_roots = [Path(p) for p in manifest.get("source_roots", [str(old_root)])]

        def relocated(value, require_file=True):
            old = Path(value)
            root = next((p for p in old_roots if old.is_relative_to(p)), None)
            if root is None:
                # Compatibility for an older same-host backup using a canonical /private/tmp root.
                old = old.resolve()
                root = next((p for p in old_roots if old.is_relative_to(p.resolve())), None)
                if root is not None:
                    root = root.resolve()
            if root is None:
                raise ValueError("对象路径超出原备份数据目录")
            relative = old.relative_to(root)
            if ".." in relative.parts:
                raise ValueError("对象引用包含越界路径")
            new = destination / relative
            if require_file and not new.is_file():
                raise ValueError("备份缺少被引用的原件")
            return str(new)

        for identifier, text in db.execute("SELECT id,pages FROM documents").fetchall():
            pages = json.loads(text)
            for page in pages:
                for key in ("path", "source"):
                    if page.get(key):
                        page[key] = relocated(page[key])
            db.execute(
                "UPDATE documents SET pages=? WHERE id=?", (json.dumps(pages, ensure_ascii=False), identifier)
            )
        if "intake" in {r[1] for r in db.execute("PRAGMA table_info(documents)")}:
            for identifier, text in db.execute(
                "SELECT id,intake FROM documents WHERE intake IS NOT NULL"
            ).fetchall():
                intake = json.loads(text)
                for item in intake.get("files", []):
                    if item.get("source"):
                        item["source"] = relocated(item["source"])
                db.execute(
                    "UPDATE documents SET intake=? WHERE id=?",
                    (json.dumps(intake, ensure_ascii=False), identifier),
                )
        for identifier, text in db.execute(
            "SELECT id,run_path FROM jobs WHERE run_path IS NOT NULL"
        ).fetchall():
            db.execute(
                "UPDATE jobs SET run_path=? WHERE id=?", (relocated(text, require_file=False), identifier)
            )
        db.execute(
            "UPDATE jobs SET state='interrupted',error='备份恢复后需显式复核/重试' WHERE state IN ('running','queued','fallback_running','fallback_queued')"
        )
        db.execute("UPDATE documents SET state='needs_human' WHERE state IN ('recognizing','queued')")
        db.commit()
        counts = {
            t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("documents", "publications", "audit", "notices")
        }
    finally:
        db.close()
    incomplete.unlink()
    return {
        "path": str(destination),
        "integrity": "ok",
        "counts": counts,
        "note": "恢复副本不会自动调用云服务；需重新配置明确授权的环境",
    }
