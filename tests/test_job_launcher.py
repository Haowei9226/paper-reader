"""Check job dispatch without using the existing library or any model service."""
import time

import pytest

from reader import jobs
from reader.store import Store


@pytest.mark.parametrize("frozen", [False, True])
def test_scheduler_dispatches_worker_with_its_own_data_directory(tmp_path, monkeypatch, frozen):
    store = Store(tmp_path / "empty-library")
    job = store.create_job("backup")
    scheduler = jobs.Scheduler(store)
    captured = {}

    class FakeProcess:
        def poll(self):
            return None

    def launch(command, **kwargs):
        captured.update(command=command, **kwargs)
        scheduler.stop.set()
        return FakeProcess()

    monkeypatch.setattr(jobs.sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(jobs.subprocess, "Popen", launch)
    monkeypatch.setattr(jobs, "ProcessGroup", lambda _process: None)
    scheduler.run()

    arguments = captured["command"]
    assert arguments[0] == jobs.sys.executable
    if frozen:
        assert arguments[1:] == ["--reader-worker", job["id"]]
        assert "-m" not in arguments
    else:
        assert arguments[1:] == ["-B", "-m", "reader.worker", job["id"]]
    assert captured["env"]["READER_DATA_DIR"] == str(store.root)
    assert captured["stdout"].name == str(store.root / "jobs.log")
    assert captured["stdout"] is captured["stderr"]
    assert store.job(job["id"])["state"] == "running"


def test_source_scheduler_completes_a_backup_in_a_separate_process(tmp_path):
    store = Store(tmp_path / "empty-library")
    job = store.create_job("backup")
    scheduler = jobs.Scheduler(store)
    scheduler.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and store.job(job["id"])["state"] in ("queued", "running"):
            time.sleep(.1)
        result = store.job(job["id"])
        assert result["state"] == "done", result["message"]
        assert (store.root / "backups" / result["result"]["filename"]).is_file()
    finally:
        scheduler.close()
