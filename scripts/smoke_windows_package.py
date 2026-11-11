"""Exercise the actual frozen Windows app using an isolated synthetic library.

The Python interpreter running this verification script is only a test driver.
The application and its workers receive a system-only PATH and no API keys.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

import httpx
import pymupdf


ROOT = Path(__file__).resolve().parent.parent
NOTE = "Portable smoke test: synthetic note, no real paper or credentials."


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def synthetic_pdf():
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=842)
        page.insert_text((48, 65), "Portable Reader Validation", fontsize=18)
        page.insert_text((48, 95), "Synthetic document - not a published paper", fontsize=10)
        page.insert_text((48, 145), "1 Introduction", fontsize=14)
        page.insert_textbox((48, 175, 540, 260),
                            "Shared representations help teams coordinate their work.\n"
                            "This synthetic passage verifies local PDF parsing and persistent notes.", fontsize=12)
        document.set_metadata({"title": "Portable Reader Validation", "author": "Synthetic validation"})
        return document.tobytes()


def free_port(first):
    for port in range(first, first + 20):
        try:
            with socket.socket() as probe:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    raise RuntimeError("No free local smoke-test port")


class AppProcess:
    def __init__(self, executable, environment, profile, port):
        self.executable, self.environment, self.profile, self.port = executable, environment, profile, port
        self.library = (profile / "PaperReader" / "data").resolve()
        self.identity = hashlib.sha256(str(self.library).encode()).hexdigest()
        self.process = None
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False)

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    def assert_identity(self):
        health = self.request("GET", "/api/health", timeout=1).json()
        require(health.get("app") == "paper-reader", "Unexpected app on smoke-test port")
        require(health.get("library_id") == self.identity, "App opened an unexpected library")
        return health

    def start(self):
        self.process = subprocess.Popen([str(self.executable), "--no-browser", "--port", str(self.port)],
                                        cwd=self.profile, env=self.environment,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Packaged application exited before becoming ready")
            try:
                return self.assert_identity()
            except httpx.HTTPError:
                time.sleep(.2)
        raise TimeoutError("Packaged application did not become ready within 60 seconds")

    def stop(self):
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                # Verify ownership by library identity before sending a quit request.
                self.assert_identity()
                require(self.request("POST", "/api/app/quit").json().get("ok"), "Quit request failed")
                self.process.wait(timeout=15)
            require(self.process.returncode == 0, "Packaged app did not exit cleanly")
        finally:
            if self.process.poll() is None:
                # Kill only this launcher's PID and descendants, including error dialogs.
                taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
                subprocess.run([str(taskkill), "/PID", str(self.process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None

    def close(self):
        try:
            self.stop()
        finally:
            self.client.close()


def validate(executable, first_port=18767):
    require(os.name == "nt", "This verification requires Windows")
    executable = executable.resolve(strict=True)
    output = ROOT / "output"
    output.mkdir(exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="smoke-", dir=output))
    excluded = {"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "READER_DATA_DIR"}
    environment = {name: value for name, value in os.environ.items() if name.upper() not in excluded}
    system_root = Path(os.environ["SystemRoot"])
    environment["PATH"] = os.pathsep.join(str(path) for path in (
        system_root / "System32", system_root,
        system_root / "System32" / "WindowsPowerShell" / "v1.0"))
    environment["LOCALAPPDATA"] = str(profile)
    app = AppProcess(executable, environment, profile, free_port(first_port))
    report = {"status": "running", "executable": str(executable),
              "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
              "isolated_profile": str(profile), "checks": []}

    def passed(name):
        report["checks"].append(name)
        print("PASS " + name, flush=True)

    try:
        health = app.start()
        report["version"] = health["version"]
        passed("packaged app starts with system-only PATH and persistent isolated data directory")
        require(app.library.is_dir(), "Persistent user data folder was not created")
        settings = app.request("GET", "/api/settings").json()
        require(settings["openai_configured"] is False and settings["deepseek_configured"] is False,
                "Fresh app unexpectedly reports an API key")
        require(settings["metadata_lookup"] is False and settings["provider"] == "local",
                "Fresh shared app does not default to local/offline settings")
        require(app.request("GET", "/api/papers").json()["total"] == 0, "Fresh app already contains papers")
        require(app.request("GET", "/api/annotations").json()["total"] == 0, "Fresh app already contains notes")
        passed("fresh library has no papers, notes or API credentials and defaults to offline metadata")
        index = app.request("GET", "/")
        require("<html" in index.text and "<script" in index.text, "Bundled web interface missing")
        passed("bundled browser interface is served")

        result = app.request("POST", "/api/import", files={"files": (
            "portable-synthetic.pdf", synthetic_pdf(), "application/pdf")}).json()
        require(len(result["imported"]) == 1 and not result["errors"], "Synthetic PDF import failed")
        paper_id = result["imported"][0]["id"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            jobs = app.request("GET", "/api/jobs").json()
            parse = next((job for job in jobs if job["paper_id"] == paper_id and job["kind"] == "parse"), None)
            if parse and parse["state"] == "done":
                break
            if parse and parse["state"] in ("failed", "partial", "cancelled"):
                raise RuntimeError("Frozen worker failed synthetic PDF parsing: " + parse["message"])
            time.sleep(.25)
        else:
            raise TimeoutError("Frozen worker did not finish PDF parsing within 90 seconds")
        page = app.request("GET", f"/api/papers/{paper_id}/pages/1").json()
        block = next((value for value in page["blocks"] if "coordinate their work" in value["text"]), None)
        require(block is not None, "Frozen parser did not produce expected synthetic text")
        passed("real frozen worker asynchronously parses synthetic PDF without Python or Node on PATH")

        start = block["text"].index("coordinate their work")
        note = app.request("POST", f"/api/papers/{paper_id}/annotations", json={
            "targets": [{"block_id": block["id"], "start": start, "end": start + len("coordinate their work")}],
            "note": NOTE}).json()
        require(note["note"] == NOTE and note["targets"][0]["rects"], "Source-linked note was not saved")
        pdf = app.request("GET", f"/api/papers/{paper_id}/export/pdf").content
        with pymupdf.open(stream=pdf, filetype="pdf") as exported:
            require(exported.page_count == 1, "Exported PDF has wrong page count")
            require(any(NOTE in annotation.info["content"] for annotation in exported[0].annots()),
                    "Exported PDF does not contain saved note")
        html = app.request("GET", f"/api/papers/{paper_id}/export/html").text
        require("<html" in html and "coordinate their work" in html, "HTML export omitted parsed source")
        require(NOTE in app.request("GET", f"/api/papers/{paper_id}/export/notes").text,
                "Notes export omitted saved note")
        passed("source-linked notes and PDF, HTML and Markdown exports work")

        app.stop()
        passed("application exits cleanly through its own quit endpoint")
        app.start()
        require(app.request("GET", "/api/papers").json()["total"] == 1, "Imported paper did not survive restart")
        notes = app.request("GET", f"/api/papers/{paper_id}/annotations").json()
        require(any(value["id"] == note["id"] and value["note"] == NOTE for value in notes),
                "Saved note did not survive restart")
        require(app.request("GET", f"/api/papers/{paper_id}").json()["parse_state"] == "ready",
                "Parsed paper did not survive restart")
        settings = app.request("GET", "/api/settings").json()
        require(not settings["openai_configured"] and not settings["deepseek_configured"],
                "Restart unexpectedly found cloud API credentials")
        passed("same isolated library, parsed text and note persist across restart")
        app.stop()
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        try:
            app.close()
        except Exception as cleanup_error:
            report["cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
            if report["status"] == "passed":
                report["status"] = "failed"
                raise
        finally:
            report_path = output / "portable-validation.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("Report: " + str(report_path), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--port", type=int, default=18767)
    args = parser.parse_args()
    validate(args.executable, args.port)
