# -*- coding: utf-8 -*-
"""梧桐河灌区模型平台（微服务版）—— 任务队列与任务存储。

模型计算耗时较长（SPAC 单作物约 1 分钟；种植结构特征栈推理数十秒到数分钟），
因此每个微服务内部采用「单工作线程 + 队列」模型：

  · HTTP 请求只排队拿 job_id，立即返回，不阻塞
  · 计算在独立线程内**串行**执行 —— 内核用 print 输出，串行才能保证日志不乱序，
    而且内核会写自己的 outputs/ 目录，并发会互相踩
  · 任务执行期间 sys.stdout/stderr 被重定向到该任务的日志缓冲区（同时旁路到
    进程真实 stdout，便于容器日志排查）
  · 前端按 offset 轮询增量日志，实现"边跑边看"
  · 任务元数据与结果落盘到 work/<job_id>/job.json，进程重启后仍能查看历史任务
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from queue import Queue

MAX_LOG_CHARS = 4_000_000        # 单任务日志上限，超出后截断头部


class _JobWriter:
    """把内核的 print() 双写：任务日志缓冲区 + 进程真实 stdout/stderr。"""

    def __init__(self, job: "Job", mirror=None):
        self.job = job
        self.mirror = mirror

    def write(self, text: str) -> int:
        if text:
            self.job.append_log(text)
            if self.mirror is not None:
                try:
                    self.mirror.write(text)
                except Exception:                        # noqa: BLE001
                    pass
        return len(text)

    def flush(self) -> None:
        if self.mirror is not None:
            try:
                self.mirror.flush()
            except Exception:                            # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False


class Job:
    """一个模型运行任务。"""

    def __init__(self, kind: str, title: str, out_dir: Path, params: dict | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind                    # spac | crop
        self.title = title
        self.params = params or {}
        self.out_dir = Path(out_dir)
        self.status = "queued"              # queued | running | done | failed
        self.progress = "排队中"
        self.created = time.time()
        self.started = 0.0
        self.finished = 0.0
        self.result: dict | None = None
        self.error: str | None = None
        self._log = ""
        self._lock = threading.Lock()

    # ---- 日志 ----
    def append_log(self, text: str) -> None:
        with self._lock:
            self._log += text
            if len(self._log) > MAX_LOG_CHARS:
                self._log = "…（日志过长, 头部已截断）…\n" + self._log[-MAX_LOG_CHARS // 2:]

    def log_since(self, offset: int) -> tuple[str, int]:
        with self._lock:
            total = len(self._log)
            offset = max(0, min(int(offset), total))
            return self._log[offset:], total

    def full_log(self) -> str:
        with self._lock:
            return self._log

    # ---- 快照 ----
    def snapshot(self, offset: int = 0) -> dict:
        chunk, total = self.log_since(offset)
        elapsed = ((self.finished or time.time()) - self.started) if self.started else 0.0
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "params": self.params,
            "status": self.status,
            "progress": self.progress,
            "log": chunk,
            "log_offset": total,
            "elapsed": round(elapsed, 1),
            "created": self.created,
            "result": self.result if self.status == "done" else None,
            "error": self.error,
        }

    # ---- 落盘 ----
    def to_json(self) -> dict:
        d = self.snapshot(0)
        d.pop("log", None)
        d["log_chars"] = len(self._log)
        return d

    @classmethod
    def from_json(cls, path: Path) -> "Job | None":
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        job = cls(d.get("kind", "?"), d.get("title", "?"), path.parent,
                  d.get("params") or {})
        job.id = d.get("id") or path.parent.name
        job.status = d.get("status", "done")
        job.progress = d.get("progress", "")
        job.created = float(d.get("created") or path.stat().st_mtime)
        job.started = float(d.get("started") or 0)
        job.finished = float(d.get("finished") or 0)
        job.result = d.get("result")
        job.error = d.get("error")
        # 历史任务的日志从同级 log.txt 恢复（若存在）
        logf = path.parent / "run.log"
        if logf.exists():
            try:
                job._log = logf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        return job


class JobManager:
    """串行任务队列（每个微服务进程一个实例）。"""

    def __init__(self, work_dir: Path, kind: str):
        self.work_dir = Path(work_dir)
        self.kind = kind
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: Queue[tuple[Job, object]] = Queue()
        self._lock = threading.Lock()
        self._load_history()
        self._worker = threading.Thread(target=self._loop, daemon=True,
                                        name=f"{kind}-worker")
        self._worker.start()

    # ---- 历史任务 ----
    def _load_history(self) -> None:
        try:
            metas = sorted(self.work_dir.glob("*/job.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for p in metas[:100]:
            job = Job.from_json(p)
            if job is None:
                continue
            # 上次进程被杀时留下的"运行中"，标成中断
            if job.status in ("running", "queued"):
                job.status = "failed"
                job.error = job.error or "服务重启，任务被中断"
                job.progress = "中断"
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._order.reverse()

    def _persist(self, job: Job) -> None:
        try:
            (job.out_dir / "job.json").write_text(
                json.dumps(job.to_json(), ensure_ascii=False, indent=2),
                encoding="utf-8")
            (job.out_dir / "run.log").write_text(job.full_log(), encoding="utf-8")
        except OSError:
            pass

    # ---- 提交 ----
    def submit(self, title: str, fn, params: dict | None = None) -> Job:
        job = Job(self.kind, title, self.work_dir, params)
        job.out_dir = self.work_dir / job.id
        job.out_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._persist(job)          # ★ 排队即落盘：进程被杀时 _load_history 才能标「中断」
        self._queue.put((job, fn))
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            ids = self._order[-limit:][::-1]
        out = []
        for i in ids:
            job = self._jobs.get(i)
            if job is None:
                continue
            s = job.snapshot(0)
            s.pop("log", None)
            out.append(s)
        return out

    def queue_len(self) -> int:
        return self._queue.qsize()

    # ---- 工作线程 ----
    def _loop(self) -> None:
        while True:
            job, fn = self._queue.get()
            job.status = "running"
            job.started = time.time()
            job.progress = "运行中"
            self._persist(job)      # ★ 开工即落盘：中途被杀 → 重启后标「服务重启，任务被中断」
            real_out, real_err = sys.stdout, sys.stderr
            sys.stdout = _JobWriter(job, real_out)
            sys.stderr = _JobWriter(job, real_err)
            try:
                job.out_dir.mkdir(parents=True, exist_ok=True)
                job.result = fn(job)
                job.status = "done"
                job.progress = "完成"
            except SystemExit as e:                      # 内核 sys.exit 兜底
                job.status = "failed"
                job.progress = "失败"
                job.error = f"内核请求退出 (code={e.code})\n请查看日志中的错误信息。"
                job.append_log(f"\n[错误] 内核 sys.exit(code={e.code})\n")
            except Exception:                            # noqa: BLE001
                job.status = "failed"
                job.progress = "失败"
                job.error = traceback.format_exc()
                job.append_log("\n[错误] 运行失败:\n" + job.error + "\n")
            finally:
                sys.stdout, sys.stderr = real_out, real_err
                job.finished = time.time()
                self._persist(job)
                self._queue.task_done()
