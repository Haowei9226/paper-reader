"""Shared builds use a separate, persistent library and cannot open a personal one."""
import hashlib
import importlib.util
from pathlib import Path
import sys

import httpx
import pytest

import reader
from reader import __main__ as entry
from reader import runtime, store, worker


def test_shared_windows_data_uses_user_profile_outside_extracted_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "SHARED_BUILD", True)
    monkeypatch.setattr(reader.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(reader, "ROOT", tmp_path / "temporary-bundle")
    assert reader.default_data_dir() == tmp_path / "Local" / "PaperReader" / "data"


def test_shared_mac_data_uses_application_support(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "SHARED_BUILD", True)
    monkeypatch.setattr(reader.sys, "platform", "darwin")
    monkeypatch.setattr(reader.Path, "home", classmethod(lambda _cls: tmp_path))
    assert reader.default_data_dir() == tmp_path / "Library" / "Application Support" / "PaperReader" / "data"


def test_source_build_keeps_existing_project_data_location(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "SHARED_BUILD", False)
    monkeypatch.setattr(reader, "ROOT", tmp_path / "source")
    assert reader.default_data_dir() == tmp_path / "source" / "data"


def test_shared_new_library_stays_offline_until_user_enables_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "SHARED_BUILD", True)
    library = store.Store(tmp_path / "clean-library")
    assert library.settings()["provider"] == "local"
    assert library.settings()["metadata_lookup"] is False
    library.set_settings({"metadata_lookup": True})
    assert store.Store(library.root).settings()["metadata_lookup"] is True


def test_shared_build_never_starts_neighboring_private_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "SHARED_BUILD", True)
    monkeypatch.setattr(runtime, "available", lambda *_args: False)
    private = tmp_path / "private-runtime"
    binary = private / "ollama" / "v0.40.13" / "ollama.exe"
    manifest = private / "models" / "manifests" / "registry.ollama.ai" / "library" / "qwen3.5" / "4b"
    binary.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    binary.write_bytes(b"not an executable")
    manifest.write_text("synthetic")
    monkeypatch.setattr(runtime, "SOURCE", private)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *_a, **_k: pytest.fail("Private runtime was started"))
    manager = runtime.Runtime(tmp_path)
    settings = {"local_url": "http://127.0.0.1:11434", "local_model": runtime.DEFAULT_MODEL}
    assert manager.status(settings)["state"] == "unavailable"
    with pytest.raises(ValueError, match="本地模型服务未就绪"):
        manager.ensure(settings)


def mock_ports(monkeypatch, info, occupied):
    probed = []

    def health(address, **_kwargs):
        port = int(address.split(":")[2].split("/")[0])
        return httpx.Response(200, json=info.get(port, {}))

    class Probe:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def bind(self, address):
            probed.append(address[1])
            if address[1] in occupied:
                raise OSError("occupied")

    monkeypatch.setattr(entry.httpx, "get", health)
    monkeypatch.setattr(entry.socket, "socket", Probe)
    return probed


def library_info(path):
    return {"app": "paper-reader", "version": entry.VERSION,
            "library_id": hashlib.sha256(str(path).encode()).hexdigest()}


def test_shared_port_skips_personal_library_and_other_apps(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "DATA", tmp_path / "shared")
    probes = mock_ports(monkeypatch, {8767: library_info(tmp_path / "personal"),
                                     8768: {"app": "unrelated-service"}}, {8767, 8768})
    assert entry.shared_port() == 8769
    assert probes == [8767, 8768, 8769]


def test_shared_port_reuses_only_same_version_and_same_library(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "DATA", tmp_path)
    probes = mock_ports(monkeypatch, {8767: library_info(tmp_path)}, {8767})
    assert entry.shared_port() == 8767
    assert probes == []


def test_shared_port_reports_exhaustion_without_changing_another_service(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "DATA", tmp_path)
    mock_ports(monkeypatch, {}, set(range(8767, 8787)))
    with pytest.raises(RuntimeError, match="端口已被占用"):
        entry.shared_port()


def test_shared_explicit_port_never_opens_different_library(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "SHARED_BUILD", True)
    monkeypatch.setattr(entry, "DATA", tmp_path / "shared")
    monkeypatch.setattr(sys, "argv", ["PaperReader.exe", "--port", "8767"])
    mock_ports(monkeypatch, {8767: library_info(tmp_path / "personal")}, {8767})
    monkeypatch.setattr(entry.webbrowser, "open", lambda *_a: pytest.fail("Opened a personal library"))
    monkeypatch.setattr(entry, "create_app", lambda **_k: pytest.fail("Attempted to replace another service"))
    with pytest.raises(RuntimeError):
        entry.main()


def test_shared_main_reuses_same_library_without_starting_a_second_server(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "SHARED_BUILD", True)
    monkeypatch.setattr(entry, "DATA", tmp_path)
    monkeypatch.setattr(sys, "argv", ["PaperReader.exe"])
    mock_ports(monkeypatch, {8767: library_info(tmp_path)}, {8767})
    opened = []
    monkeypatch.setattr(entry.webbrowser, "open", opened.append)
    monkeypatch.setattr(entry, "create_app", lambda **_k: pytest.fail("Started a duplicate server"))
    entry.main()
    assert opened == ["http://127.0.0.1:8767"]


def test_desktop_worker_dispatches_without_loading_browser_and_redirects_logs(tmp_path, monkeypatch):
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "packaging" / "desktop_entry.py"
    monkeypatch.setenv("READER_SHARED_BUILD", "0")
    spec = importlib.util.spec_from_file_location("shared_desktop_test", launcher)
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    assert desktop.os.environ["READER_SHARED_BUILD"] == "1"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "argv", ["PaperReader.exe", "--reader-worker", "synthetic-job"])
    monkeypatch.setattr(reader, "DATA", tmp_path / "persistent-data")
    sentinel = object()
    monkeypatch.setattr(store, "Store", lambda: sentinel)
    seen = []

    def run(library, job_id):
        seen.append((library, job_id))
        print("synthetic worker diagnostic")

    monkeypatch.setattr(worker, "run", run)
    monkeypatch.setattr(entry, "main", lambda: pytest.fail("Worker opened the browser/server"))
    assert desktop.launch() == 0
    assert seen == [(sentinel, "synthetic-job")]
    assert "synthetic worker diagnostic" in (reader.DATA / "jobs.log").read_text(encoding="utf-8")
