from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import ROOT, SHARED_BUILD
from .processes import ProcessGroup

SOURCE = ROOT.parent / "蓝色大肥鱼" / "runtime" / "local-ai"
DEFAULT_MODEL = "qwen3.5:4b"


def local_url(value: str):
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("本地模型地址须为本机 HTTP 地址，例如 http://127.0.0.1:11435")
    return value.rstrip("/")


def available(url, model=DEFAULT_MODEL):
    try:
        response = httpx.get(local_url(url) + "/api/tags", timeout=1, trust_env=False)
        return response.status_code == 200 and any(m.get("name") == model for m in response.json().get("models", []))
    except (httpx.HTTPError, ValueError):
        return False


class Runtime:
    def __init__(self, root):
        self.root = root / "local-ai"
        self.process = None
        self.group = None
        self.lock = threading.Lock()

    def status(self, settings):
        model = settings["local_model"]
        for url in dict.fromkeys([settings["local_url"], "http://127.0.0.1:11436", "http://127.0.0.1:11434"]):
            if available(url, model):
                return {"state": "ready", "url": url, "model": model}
        runtime = SOURCE / "ollama" / "v0.40.13" / "ollama.exe"
        manifest = SOURCE / "models" / "manifests" / "registry.ollama.ai" / "library" / "qwen3.5" / "4b"
        return {"state": "stopped" if not SHARED_BUILD and os.name == "nt" and runtime.exists() and manifest.exists() else "unavailable", "url": settings["local_url"], "model": model}

    def ensure(self, settings):
        with self.lock:
            status = self.status(settings)
            if status["state"] == "ready":
                return status
            if status["state"] == "unavailable" or settings["local_model"] != DEFAULT_MODEL:
                raise ValueError("本地模型服务未就绪，请在设置中填写正在运行的 Ollama 地址")
            self.root.mkdir(exist_ok=True)
            home = self.root / "home"
            temp = self.root / "tmp"
            home.mkdir(exist_ok=True)
            temp.mkdir(exist_ok=True)
            environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("OLLAMA_")}
            environment.update({"OLLAMA_HOST": "127.0.0.1:11436", "OLLAMA_MODELS": str(SOURCE / "models"),
                                "OLLAMA_NO_CLOUD": "1", "OLLAMA_NO_PRUNE": "1", "OLLAMA_NOHISTORY": "1",
                                "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_KEEP_ALIVE": "5m",
                                "OLLAMA_CONTEXT_LENGTH": str(settings["context_tokens"]),
                                "HOME": str(home), "USERPROFILE": str(home), "TMP": str(temp), "TEMP": str(temp)})
            log = (self.root / "runtime.log").open("ab")
            try:
                self.process = subprocess.Popen([str(SOURCE / "ollama" / "v0.40.13" / "ollama.exe"), "serve"],
                    env=environment, cwd=self.root, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                try:
                    self.group = ProcessGroup(self.process)
                except OSError:
                    self.process.terminate()
                    raise ValueError("系统未允许管理本地模型进程，请连接已运行的 Ollama 服务")
            finally:
                log.close()
            for _ in range(40):
                if available("http://127.0.0.1:11436", DEFAULT_MODEL):
                    return {"state": "ready", "url": "http://127.0.0.1:11436", "model": DEFAULT_MODEL}
                if self.process.poll() is not None:
                    break
                time.sleep(.25)
            raise ValueError("本地 Ollama 未能启动，请在设置中检查服务地址")

    def close(self):
        if self.group:
            self.group.close()
            self.group = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
