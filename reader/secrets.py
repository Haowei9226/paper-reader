"""User-scoped OS credential storage; never return API credentials to the UI."""
import ctypes
import hashlib
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

from . import ROOT

CLOUD_NAMES = {"deepseek": "DeepSeek", "openai": "OpenAI"}
KEYCHAIN_NOT_FOUND = 44  # errSecItemNotFound (-25300) as a process exit status.


def _keychain_quote(value: str) -> str:
    # security -i has its own quoted-argument parser, not a shell parser.
    # Apple SecurityTool/macOS/security.c: split_line escapes the next character.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _keychain_command(arguments, *, action: str, input: str | None = None):
    try:
        return subprocess.run(["/usr/bin/security", *arguments], input=input,
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise ValueError(f"macOS 钥匙串无法{action} API Key，请解锁登录钥匙串并允许访问后重试") from None


def _keychain_failure(action: str):
    # Never include security output: a failed operation may contain secret data.
    raise ValueError(f"macOS 钥匙串无法{action} API Key，请解锁登录钥匙串并允许访问后重试")


def local_openai_key():
    # The secure setup tool writes this ignored, project-local file.
    try:
        with (ROOT / ".env.local").open(encoding="utf-8-sig") as source:
            for line in source:
                name, sep, value = line.strip().removeprefix("export ").partition("=")
                if sep and name.strip() == "OPENAI_API_KEY":
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    return value
    except (OSError, UnicodeError):
        pass
    return ""


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("此系统请通过环境变量提供 API Key")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "PaperReader", None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise RuntimeError("Windows 无法加密或解密密钥，请重新保存")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(output.data)


class Secrets:
    def __init__(self, root: Path, provider: str = "deepseek"):
        if provider not in CLOUD_NAMES:
            raise ValueError("不支持此密钥服务")
        self.provider = provider
        self.path = root / f"{provider}.key.dpapi"
        library = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:32]
        self.keychain_service = "org.paper-reader.api." + library

    def _keychain_arguments(self, command):
        return [command, "-s", self.keychain_service, "-a", self.provider]

    def get(self):
        if sys.platform == "darwin":
            result = _keychain_command(self._keychain_arguments("find-generic-password") + ["-w"], action="读取")
            if result.returncode == 0:
                return result.stdout.rstrip("\r\n")
            if result.returncode != KEYCHAIN_NOT_FOUND:
                _keychain_failure("读取")
        elif self.path.exists():
            try:
                return _crypt(self.path.read_bytes(), decrypt=True).decode()
            except (OSError, RuntimeError, UnicodeError):
                pass
        configured = os.environ.get(f"{self.provider.upper()}_API_KEY", "").strip()
        return configured or (local_openai_key() if self.provider == "openai" else "")

    def present(self):
        return bool(self.get())

    def save(self, key: str):
        if not isinstance(key, str):
            raise ValueError("API Key 格式不正确")
        key = key.strip()
        if not key:
            return
        if len(key) > 1000 or any(ch.isspace() for ch in key):
            raise ValueError("API Key 格式不正确")
        if sys.platform == "darwin":
            # A batch newline would start another command; NUL and non-ASCII
            # credentials cannot round-trip reliably through security's CLI.
            if any(not 33 <= ord(ch) <= 126 for ch in key):
                raise ValueError("API Key 格式不正确，请使用无空白的可打印英文字符")
            arguments = self._keychain_arguments("add-generic-password") + ["-U", "-w", key]
            command = " ".join(_keychain_quote(value) for value in arguments) + "\n"
            # stdin keeps the key out of process arguments, shell history and files.
            result = _keychain_command(["-i"], action="保存", input=command)
            if result.returncode:
                _keychain_failure("保存")
            return
        encrypted = _crypt(key.encode())
        temp = self.path.with_suffix(".tmp")
        temp.write_bytes(encrypted)
        os.replace(temp, self.path)

    def clear(self):
        if sys.platform == "darwin":
            result = _keychain_command(self._keychain_arguments("delete-generic-password"), action="删除")
            if result.returncode not in (0, KEYCHAIN_NOT_FOUND):
                _keychain_failure("删除")
            return
        self.path.unlink(missing_ok=True)
