import json

from reader import runtime


def test_shared_runtime_ignores_developer_environment_and_local_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setattr(runtime, "SHARED_BUILD", True)
    monkeypatch.setenv("READER_LOCAL_RUNTIME_DIR", str(tmp_path / "private-environment"))
    (tmp_path / ".reader-local.json").write_text(json.dumps({"local_runtime_dir": str(tmp_path / "private-file")}), encoding="utf-8")
    assert runtime.runtime_source() == tmp_path / "runtime" / "local-ai"


def test_developer_runtime_environment_overrides_local_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setattr(runtime, "SHARED_BUILD", False)
    monkeypatch.setenv("READER_LOCAL_RUNTIME_DIR", str(tmp_path / "custom-runtime"))
    assert runtime.runtime_source() == (tmp_path / "custom-runtime").resolve()


def test_developer_runtime_file_is_local_and_optional(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setattr(runtime, "SHARED_BUILD", False)
    monkeypatch.delenv("READER_LOCAL_RUNTIME_DIR", raising=False)
    (tmp_path / ".reader-local.json").write_text(json.dumps({"local_runtime_dir": str(tmp_path / "custom-runtime")}), encoding="utf-8")
    assert runtime.runtime_source() == (tmp_path / "custom-runtime").resolve()
    (tmp_path / ".reader-local.json").write_text("not-json", encoding="utf-8")
    assert runtime.runtime_source() == tmp_path / "runtime" / "local-ai"
