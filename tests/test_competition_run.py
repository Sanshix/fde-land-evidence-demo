"""The live runner is opt-in, isolated, and cannot disguise recognition as human review."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import competition_run as runner


def make_inputs(tmp_path):
    samples = tmp_path / "模拟材料"
    images = samples / "01_合同扫描件"
    images.mkdir(parents=True)
    for name in runner.SOURCE_FILES:
        (samples / name).write_bytes(b"business-workbook-placeholder")
    (images / "LX-001_1.jpg").write_bytes(b"source-one")
    (images / "LX-001_2.jpg").write_bytes(b"source-two")
    (images / "LX-002_1.jpg").write_bytes(b"source-three")
    return samples


def test_module_import_does_not_initialize_runtime(tmp_path):
    module = Path(runner.__file__).resolve()
    script = """
import importlib.util, json, os, pathlib, sys
before = dict(os.environ)
spec = importlib.util.spec_from_file_location('competition_import_test', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps({'env_unchanged':dict(os.environ)==before,'app_imported':'app.main' in sys.modules,'files':list(pathlib.Path.cwd().iterdir())}, default=str))
"""
    result = subprocess.run([sys.executable, "-c", script, str(module)], cwd=tmp_path,
                            check=True, text=True, capture_output=True)
    assert json.loads(result.stdout) == {"env_unchanged": True, "app_imported": False, "files": []}


def test_live_gate_precedes_any_file_or_environment_mutation(tmp_path):
    args = SimpleNamespace(live=False, run=tmp_path / "missing")
    before = dict(os.environ)
    with pytest.raises(ValueError, match="--live"):
        runner.run(args)
    assert dict(os.environ) == before
    assert not list(tmp_path.iterdir())


def test_run_directory_is_fresh_and_scoped(tmp_path):
    root = tmp_path / "project"
    (root / "runs").mkdir(parents=True)
    assert runner.validate_run_path(root / "runs/new", root=root) == root / "runs/new"
    for bad in (root / "runs", tmp_path / "elsewhere", root / "runs/../../outside"):
        with pytest.raises(ValueError):
            runner.validate_run_path(bad, root=root)
    (root / "runs/existing").mkdir()
    with pytest.raises(ValueError, match="--resume"):
        runner.validate_run_path(root / "runs/existing", root=root)
    (root / "runs/escape").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        runner.validate_run_path(root / "runs/escape/child", root=root)
    with pytest.raises(ValueError, match="manifest"):
        runner.validate_run_path(root / "runs/existing", root=root, resume=True)


def test_config_keeps_authorization_region_and_env_bindings():
    profile = {"provider": "synthetic", "model_id": "synthetic-model", "region": "Beijing",
               "base_url": "https://example.invalid/v1", "api_key_env": "PROJECT_API_KEY",
               "secret_key_env": "PROJECT_SECRET_KEY", "authorized": True, "identity_verified": False}
    config = {"enabled": False, "authorized_scope": "competition_simulated", "models": {"primary": profile}}
    copied = runner.runtime_config(config)
    assert copied["enabled"] and not config["enabled"]
    assert copied["models"]["primary"] == profile
    assert copied["models"]["primary"]["identity_verified"] is False
    for key in runner.SECRET_KEYS:
        with pytest.raises(ValueError, match="内联凭据"):
            runner.runtime_config({**config, "models": {"primary": {**profile, key: "must-not-copy"}}})
    for url in ("https://user:password@example.invalid/v1", "https://example.invalid/v1?token=hidden"):
        with pytest.raises(ValueError, match="供应商地址"):
            runner.runtime_config({**config, "models": {"primary": {**profile, "base_url": url}}})
    with pytest.raises(ValueError, match="模拟"):
        runner.runtime_config({**config, "authorized_scope": "production"})


def test_input_manifest_never_reads_answer_directory(tmp_path, monkeypatch):
    samples = make_inputs(tmp_path)
    answers = samples / "08_标准答案"
    answers.mkdir()
    (answers / "合同要素.csv").write_text("must not read", encoding="utf-8")
    original = Path.read_bytes

    def guarded(path):
        assert "08_标准答案" not in path.parts
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded)
    groups = runner.collect_inputs(samples, ["LX-001"])
    manifest = runner.source_manifest(samples, groups)
    assert list(groups) == ["LX-001"]
    assert len(manifest) == 6
    assert all("答案" not in name for name in manifest)
    with pytest.raises(ValueError, match="档案号"):
        runner.collect_inputs(samples, ["LX-not-present"])


def test_duplicate_runner_cannot_take_live_directory(tmp_path):
    with runner.run_lock(tmp_path):
        with pytest.raises(ValueError, match="活动进程"):
            with runner.run_lock(tmp_path):
                pytest.fail("duplicate run acquired lock")


def test_summary_reports_partial_unknown_without_publishing(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = sqlite3.connect(data / "fde.sqlite")
    db.execute("CREATE TABLE jobs(id TEXT,document_id TEXT,state TEXT,created REAL)")
    db.execute("INSERT INTO jobs VALUES('job-one','doc-one','unknown',0)")
    db.commit()
    db.close()

    @contextmanager
    def connection():
        opened = sqlite3.connect(data / "fde.sqlite")
        opened.row_factory = sqlite3.Row
        try:
            yield opened
        finally:
            opened.close()

    document = {"id": "doc-one", "name": "LX-001", "state": "needs_human", "pages": [{}, {}],
                "result": {"completion_status": "partial", "page_coverage": {"completed": ["page-one"], "incomplete": ["page-two"]},
                           "unknown_attempts": [{"page_id": "page-two", "stage": "primary"}], "candidates": {"tenant": [{"value": "sample"}]}}}
    attempts = data / "runs/doc-one"
    attempts.mkdir(parents=True)
    runner.write_json(attempts / "fingerprint.attempt-1.json", {"status": "unknown", "sent": True})
    runner.write_json(attempts / "fingerprint.attempt-1.response.json", {"raw_response_text": "partial"})
    store = SimpleNamespace(DATA=data, connection=connection, list_docs=lambda: [document])
    summary = runner.summarize(store, tmp_path, elapsed=17.5, budget_exhausted=False, worker_errors=[])
    assert summary["published"] == 0
    assert summary["human_confirmation_simulated"] is False
    assert summary["business_acceptance"] is False
    assert summary["recognition_accepted"] is False
    assert summary["completed_pages"] == 1 and summary["pages"] == 2
    assert summary["sent_provider_attempts"] == 1
    assert summary["documents_with_unknown_attempts"] == 1
    assert summary["cost"] is None
    assert (tmp_path / "recognition/LX-001/result.json").is_file()
    assert (tmp_path / "document-details.csv").is_file()
