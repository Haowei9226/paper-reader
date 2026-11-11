"""Exercise macOS credential integration with a simulated security executable."""
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from reader import secrets as credentials


@pytest.fixture
def mac_keychain(monkeypatch):
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "local_openai_key", lambda: "")
    entries, calls = {}, []

    def run(arguments, **options):
        calls.append((arguments, options))
        command = shlex.split(options["input"]) if arguments[1:] == ["-i"] else arguments[1:]
        service = command[command.index("-s") + 1]
        account = command[command.index("-a") + 1]
        slot = service, account
        status, value = 0, ""
        if command[0] == "add-generic-password":
            assert "-U" in command
            entries[slot] = command[command.index("-w") + 1]
        elif command[0] == "find-generic-password":
            status = 0 if slot in entries else credentials.KEYCHAIN_NOT_FOUND
            value = entries.get(slot, "") + "\n"
        elif command[0] == "delete-generic-password":
            status = 0 if slot in entries else credentials.KEYCHAIN_NOT_FOUND
            entries.pop(slot, None)
        else:
            pytest.fail("Unexpected security command")
        return SimpleNamespace(returncode=status, stdout=value, stderr="")

    monkeypatch.setattr(credentials.subprocess, "run", run)
    return entries, calls


def test_mac_keys_stay_in_keychain_and_are_isolated_by_library_and_provider(tmp_path, mac_keychain):
    entries, calls = mac_keychain
    local = credentials.Secrets(tmp_path / "library", "openai")
    other_provider = credentials.Secrets(tmp_path / "library", "deepseek")
    other_library = credentials.Secrets(tmp_path / "another-library", "openai")
    local.save("synthetic-openai-key")
    other_provider.save("synthetic-deepseek-key")
    other_library.save("synthetic-other-library-key")
    assert local.get() == "synthetic-openai-key"
    assert local.present() is True
    assert other_provider.get() == "synthetic-deepseek-key"
    assert other_library.get() == "synthetic-other-library-key"
    local.save("synthetic-replacement")
    assert local.get() == "synthetic-replacement"
    local.clear()
    assert local.get() == ""
    local.clear()  # Missing keys are already cleared.
    assert other_provider.get() == "synthetic-deepseek-key"
    assert other_library.get() == "synthetic-other-library-key"
    assert len(entries) == 2
    assert list(tmp_path.rglob("*")) == []
    for arguments, options in calls:
        assert arguments[0] == "/usr/bin/security"
        assert "synthetic" not in " ".join(arguments)
        assert options["capture_output"] is True
        assert options["timeout"] == 20
        assert options.get("shell") is not True


def test_mac_batch_quotes_special_characters_without_starting_another_command(tmp_path, mac_keychain):
    _entries, calls = mac_keychain
    secret = credentials.Secrets(tmp_path)
    key = 'synthetic-\\"\'`;$(touch-do-not-run)&|<>-key'
    secret.save(key)
    assert secret.get() == key
    arguments, options = calls[0]
    assert arguments == ["/usr/bin/security", "-i"]
    assert options["input"].count("\n") == 1
    assert options["input"].endswith("\n")


@pytest.mark.parametrize("key", ["synthetic\nsecond-command", "synthetic\rnext", "synthetic\0key", "synthetic\x1bkey", "synthetic-é", "synthetic key", "k" * 1001])
def test_mac_rejects_values_that_cannot_round_trip_safely(tmp_path, mac_keychain, key):
    _entries, calls = mac_keychain
    with pytest.raises(ValueError, match="API Key 格式不正确"):
        credentials.Secrets(tmp_path).save(key)
    assert calls == []


def test_missing_mac_key_falls_back_to_environment(tmp_path, mac_keychain, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-environment-key")
    secret = credentials.Secrets(tmp_path, "openai")
    assert secret.get() == "synthetic-environment-key"
    secret.save("synthetic-saved-key")
    assert secret.get() == "synthetic-saved-key"


@pytest.mark.parametrize("method,word", [("get", "读取"), ("save", "保存"), ("clear", "删除")])
def test_mac_keychain_failure_is_clear_and_does_not_include_secret_output(tmp_path, mac_keychain, monkeypatch, method, word):
    monkeypatch.setattr(credentials.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=36, stdout="synthetic-sensitive-output", stderr="synthetic-sensitive-error"))
    with pytest.raises(ValueError, match="钥匙串无法" + word) as error:
        getattr(credentials.Secrets(tmp_path), method)(*(["synthetic-key"] if method == "save" else []))
    assert "synthetic" not in str(error.value)


def test_mac_keychain_timeout_is_reported_without_falling_back_to_file(tmp_path, mac_keychain, monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["/usr/bin/security", "-i"], 20, output="synthetic-sensitive-output")
    monkeypatch.setattr(credentials.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="钥匙串无法保存") as error:
        credentials.Secrets(tmp_path).save("synthetic-key")
    assert "synthetic" not in str(error.value)
    assert list(tmp_path.rglob("*")) == []
