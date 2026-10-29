"""Live recognition benchmark using the product's foreground/fallback job queues.

No answers are loaded and no human confirmation is simulated. Importing this module
has no environment, database, network, or filesystem side effects.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_STATES = {"queued", "running", "fallback_queued", "fallback_running"}
SOURCE_FILES = ("02_不动产登记导出.xlsx", "03_租金缴费记录.xlsx", "04_租金减免登记.xlsx", "05_计租标准与土地级别.xlsx")
SECRET_KEYS = {"api_key", "secret_key", "access_token", "token", "password", "authorization", "client_secret"}


def write_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runtime_config(config):
    """Preserve endpoint/region/env bindings, but reject embedded credential values."""
    def inspect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in SECRET_KEYS and item:
                    raise ValueError("供应商配置含内联凭据；请改用项目环境变量绑定")
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
    inspect(config)
    if config.get("authorized_scope") not in {"fictional_exercise", "competition_simulated"}:
        raise ValueError("本工具仅支持已声明的模拟练习集/比赛材料范围")
    for profile in config.get("models", {}).values():
        if not profile:
            continue
        url = urlsplit(profile.get("base_url", ""))
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("供应商地址不能含凭据、查询参数或片段")
    return {**config, "enabled": True}


def validate_run_path(path, *, resume=False, root=ROOT):
    path = Path(path).expanduser().resolve()
    allowed = (Path(root) / "runs").resolve()
    if path == allowed or not path.is_relative_to(allowed):
        raise ValueError("运行目录必须是项目 runs/ 下的独立子目录")
    if resume:
        if not (path / "manifest.json").is_file() or not (path / "data/fde.sqlite").is_file():
            raise ValueError("恢复目录缺少 manifest.json 或已初始化的隔离数据库")
    elif path.exists():
        raise ValueError("运行目录已存在；如需恢复请显式使用 --resume")
    return path


def collect_inputs(samples, names=None):
    """Only business images and four source workbooks; never scan answer folders."""
    samples = Path(samples).expanduser().resolve()
    if any(not (samples / filename).is_file() for filename in SOURCE_FILES):
        raise ValueError("材料目录缺少登记/缴费/减免/计租标准业务输入")
    groups = {}
    for path in sorted((samples / "01_合同扫描件").glob("*.jpg")):
        name = path.stem.split("_", 1)[0]
        if name in {"", ".", ".."} or any(c in name for c in "/\\"):
            raise ValueError("档案号不能构成文件路径")
        groups.setdefault(name, []).append(path)
    if not groups:
        raise ValueError("未找到合同扫描图片")
    selected = set(names or groups)
    if selected - groups.keys():
        raise ValueError("指定档案号不在合同扫描件中：" + "、".join(sorted(selected - groups.keys())))
    return {name: files for name, files in groups.items() if name in selected}


def source_manifest(samples, groups):
    samples = Path(samples).resolve()
    paths = [samples / filename for filename in SOURCE_FILES]
    paths += [path for files in groups.values() for path in files]
    return {str(path.relative_to(samples)): sha(path) for path in paths}


def code_manifest(root=ROOT):
    root = Path(root)
    paths = [p for p in (root / "app").rglob("*") if p.is_file() and p.suffix in {".py", ".js", ".html", ".css"}]
    paths.append(Path(__file__))
    return {str(path.relative_to(root)): sha(path) for path in paths}


@contextmanager
def run_lock(run):
    import fcntl

    with (Path(run) / "runner.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("该运行目录已有活动进程，不可重复启动") from error
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def summarize(store, run, *, elapsed, budget_exhausted, worker_errors):
    docs = store.list_docs()
    with store.connection() as db:
        jobs = [dict(row) for row in db.execute("SELECT id,document_id,state FROM jobs ORDER BY created")]
    job_by_doc = {}
    for job in jobs:
        job_by_doc.setdefault(job["document_id"], []).append(job)
    rows = []
    attempt_states = Counter()
    sent_attempts = 0
    for doc in sorted(docs, key=lambda item: item["name"]):
        result = doc.get("result") or {}
        coverage = result.get("page_coverage") or {}
        attempts = []
        for path in (store.DATA / "runs" / doc["id"]).glob("*.json"):
            if re.search(r"\.attempt-\d+\.json$", path.name):
                attempts.append(json.loads(path.read_text()))
        attempt_states.update(item.get("status", "unknown") for item in attempts)
        sent_attempts += sum(bool(item.get("sent")) for item in attempts)
        row = {
            "name": doc["name"], "document_id": doc["id"], "state": doc["state"],
            "completion_status": result.get("completion_status", "not_completed"),
            "pages": len(doc["pages"]), "completed_pages": len(coverage.get("completed", [])),
            "incomplete_pages": coverage.get("incomplete", []),
            "unknown_attempts": result.get("unknown_attempts", []), "attempts": len(attempts),
            "candidate_count": sum(len(values) for values in result.get("candidates", {}).values()),
            "elapsed_seconds": result.get("elapsed_seconds"),
            "job_states": [job["state"] for job in job_by_doc.get(doc["id"], [])],
            "published": doc["state"] == "published",
            "queue_wait_seconds": result.get("queue_wait_seconds"),
            "phase_execution_seconds": result.get("phase_execution_seconds"),
            "exceeded_document_expectation": result.get("exceeded_document_expectation", False),
        }
        rows.append(row)
        evidence = Path(run) / "recognition" / doc["name"]
        evidence.mkdir(parents=True, exist_ok=True)
        write_json(evidence / "outcome.json", row)
        if result:
            write_json(evidence / "result.json", result)
    state_counts = Counter(row["state"] for row in rows)
    completion_counts = Counter(row["completion_status"] for row in rows)
    summary = {
        "run": str(run), "elapsed_seconds": round(elapsed, 3), "documents": len(rows),
        "document_states": dict(state_counts), "completion_states": dict(completion_counts),
        "job_states": dict(Counter(job["state"] for job in jobs)),
        "pages": sum(row["pages"] for row in rows),
        "completed_pages": sum(row["completed_pages"] for row in rows),
        "documents_with_unknown_attempts": sum(bool(row["unknown_attempts"]) for row in rows),
        "provider_attempt_states": dict(attempt_states), "sent_provider_attempts": sent_attempts,
        "published": state_counts.get("published", 0), "human_confirmation_simulated": False,
        "recognition_accepted": len(rows) > 0 and completion_counts.get("completed", 0) == len(rows)
                                and not any(job["state"] in ACTIVE_STATES for job in jobs)
                                and not budget_exhausted and not any(row["unknown_attempts"] for row in rows),
        "business_acceptance": False, "cost": None,
        "cost_note": "未配置经核实的单价；以供应商账单核对。provider attempt 不等于物理HTTP次数（百度含鉴权/轮询）。",
        "budget_exhausted": budget_exhausted, "worker_errors": worker_errors,
        "note": "读取协议完成不等于字段正确、证据通过或真人复核完成；答案仅能在后续离线评分使用。",
    }
    write_json(Path(run) / "document-details.json", rows)
    with (Path(run) / "document-details.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        columns = ["name", "document_id", "state", "completion_status", "pages", "completed_pages", "candidate_count", "attempts", "elapsed_seconds", "published"]
        writer = csv.DictWriter(stream, columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    summary["expected_seconds"] = 1200
    summary["exceeded_expected"] = elapsed > 1200
    write_json(Path(run) / "summary.json", summary)
    return summary


def run(args):
    if not args.live:
        raise ValueError("真实调用必须显式传 --live；本工具没有伪造识别成功的离线模式")
    run_dir = validate_run_path(args.run, resume=args.resume)
    groups = collect_inputs(args.samples, args.documents)
    samples = Path(args.samples).expanduser().resolve()
    hashes = source_manifest(samples, groups)
    if args.resume:
        manifest = json.loads((run_dir / "manifest.json").read_text())
        if manifest.get("source_hashes") != hashes or manifest.get("samples") != str(samples):
            raise ValueError("恢复输入与原运行不一致；请使用原参数或创建新运行")
        config = runtime_config(json.loads((run_dir / "providers.json").read_text()))
    else:
        config = runtime_config(json.loads(Path(args.providers).expanduser().read_text()))
        run_dir.mkdir(parents=True)
        manifest = {"format": 1, "samples": str(samples), "source_hashes": hashes,
                    "created_at": time.time(), "documents": sorted(groups),
                    "scope": config["authorized_scope"], "answers_read": False,
                    "queue_design": "configured parallel foreground and fallback workers"}
    with run_lock(run_dir):
        write_json(run_dir / "providers.json", config)
        write_json(run_dir / "manifest.json", manifest)
        write_json(run_dir / f"code-{time.time_ns()}.json", code_manifest())
        os.environ.update(FDE_DATA_DIR=str(run_dir / "data"), FDE_SAMPLES_DIR=str(samples),
                          FDE_PROVIDERS_CONFIG=str(run_dir / "providers.json"), FDE_DISABLE_WORKER="1")
        sys.path.insert(0, str(ROOT))
        # Application imports must follow the isolated runtime environment above.
        from fastapi.testclient import TestClient

        from app import main as application
        from app import store

        stop = threading.Event()
        errors = []
        started = time.monotonic()



        def worker(fallback):
            try:
                while not stop.is_set():
                    if not application.worker_once(fallback=fallback, stop=stop):
                        stop.wait(.1)
            except Exception as error:
                errors.append({"fallback": fallback, "error_type": type(error).__name__})
                stop.set()

        with TestClient(application.app):
            inventory_path = run_dir / "documents.json"
            if not args.resume:
                documents = []
                for name, files in groups.items():
                    imported = application.import_files([(path.name, path.read_bytes()) for path in files], name,
                                                        "competition-live-import")
                    documents.append({"name": name, "id": imported["id"], "files": [path.name for path in files]})
                write_json(inventory_path, documents)
            from app.cloud_attempts import worker_count
            manifest["workers"] = {"primary": worker_count(), "fallback": worker_count(True)}
            write_json(run_dir / "manifest.json", manifest)
            threads = application.worker_threads(worker)
            for thread in threads:
                thread.start()
            exhausted = False
            try:
                while not stop.wait(.25):
                    with store.connection() as db:
                        active = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','fallback_queued','fallback_running')").fetchone()[0]
                    if not active:
                        break
                    # Expected duration is an observation threshold, never a cutoff.
            finally:
                stop.set()
                # Finish every selected job; expected duration does not cancel work.
                for thread in threads:
                    thread.join()
            summary = summarize(store, run_dir, elapsed=time.monotonic() - started,
                                budget_exhausted=exhausted, worker_errors=errors)
            summary["expected_seconds"] = args.budget
            summary["exceeded_expected"] = summary["elapsed_seconds"] > args.budget
            write_json(run_dir / "summary.json", summary)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            return 0 if summary["recognition_accepted"] and not errors else 2


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--samples", type=Path, required=True)
    result.add_argument("--providers", type=Path, default=ROOT / "config/providers.competition.json")
    result.add_argument("--run", type=Path, required=True)
    result.add_argument("--documents", nargs="+")
    result.add_argument("--budget", type=float, default=1200, help="整批执行预期秒数，仅记录超预期，不终止任务")
    result.add_argument("--resume", action="store_true")
    result.add_argument("--live", action="store_true")
    return result


def cli(argv=None):
    args = parser().parse_args(argv)
    if args.budget <= 0:
        parser().error("--budget 执行预期必须为正数")
    try:
        return run(args)
    except (ValueError, OSError) as error:
        print(f"运行未完成：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
