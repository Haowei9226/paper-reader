from __future__ import annotations

import os
import subprocess
import sys
import threading

from . import ROOT
from .store import now
from .processes import ProcessGroup


class Scheduler:
    def __init__(self, store):
        self.store = store
        self.stop = threading.Event()
        self.process = None
        self.group = None
        self.active_id = None
        with store.connect() as db:
            db.execute("UPDATE jobs SET state='paused',message='上次处理已中断，可继续任务',updated=? WHERE state='running'", (now(),))
        self.thread = threading.Thread(target=self.run, name="reader-jobs", daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            with self.store.connect() as db:
                if self.process:
                    row = db.execute("SELECT state FROM jobs WHERE id=?", (self.active_id,)).fetchone()
                    if not row or row[0] != "running":
                        if self.process.poll() is None:
                            self.process.terminate()
                    code = self.process.poll()
                    if code is not None:
                        if self.group:
                            self.group.close()
                            self.group = None
                        if row and row[0] == "running":
                            db.execute("UPDATE jobs SET state='failed',message='处理进程意外退出，可以重试',updated=? WHERE id=?", (now(), self.active_id))
                        self.process, self.active_id = None, None
                if not self.process:
                    job = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
                    if job and db.execute("UPDATE jobs SET state='running',updated=? WHERE id=? AND state='queued'",
                                          (now(), job["id"])).rowcount:
                        db.commit()
                        env = dict(os.environ, READER_DATA_DIR=str(self.store.root), PYTHONDONTWRITEBYTECODE="1")
                        # A frozen executable is the launcher, not a Python interpreter.
                        command = ([sys.executable, "--reader-worker", job["id"]] if getattr(sys, "frozen", False)
                                   else [sys.executable, "-B", "-m", "reader.worker", job["id"]])
                        log = (self.store.root / "jobs.log").open("ab")
                        try:
                            self.process = subprocess.Popen(command, cwd=ROOT,
                                env=env, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                            self.group = ProcessGroup(self.process)
                            self.active_id = job["id"]
                        except OSError:
                            if self.process and self.process.poll() is None:
                                self.process.terminate()
                            self.process = None
                            db.execute("UPDATE jobs SET state='failed',message='无法启动处理进程',updated=? WHERE id=?", (now(), job["id"]))
                        finally:
                            log.close()
            self.stop.wait(.4)

    def close(self):
        self.stop.set()
        if self.process and self.process.poll() is None:
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET state='paused',message='程序关闭，已保留进度',updated=? WHERE id=? AND state='running'", (now(), self.active_id))
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.group:
            self.group.close()
            self.group = None
        if self.thread.is_alive():
            self.thread.join(timeout=5)
