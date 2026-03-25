"""构建任务队列（线程安全）：per-repo FIFO 槽 + 取消超时旧任务。"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class BuildJob:
    repo: str
    pr_number: int
    head_sha: str
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: Literal["pending", "running", "done", "cancelled"] = "pending"
    created_at: float = field(default_factory=time.monotonic)
    # PR 评论 ID（由 Reporter.on_enqueue 写入，供后续编辑同一条评论）
    comment_id: int | None = None


class JobQueue:
    """
    任务队列。调度规则：
      - 同一 repo 的 job 串行（同 repo 最多一个 running）
      - 不同 repo 的 job 可并行（受外部 MAX_PARALLEL_JOBS 限制）
      - cancel_superseded=True 时，同 PR 入队新 job 会将旧 pending job 标记为 cancelled
    """

    def __init__(self, cancel_superseded: bool = True) -> None:
        self._pending: deque[BuildJob] = deque()
        self._running: list[BuildJob] = []
        self._lock = threading.Lock()
        self.cancel_superseded = cancel_superseded

    # ── 入队 ──────────────────────────────────────────────────────────────

    def enqueue(self, job: BuildJob) -> None:
        """追加到队列末尾；若 cancel_superseded 则先取消同 PR 的旧 pending job。"""
        with self._lock:
            if self.cancel_superseded:
                self._cancel_for_pr_locked(job.repo, job.pr_number)
            self._pending.append(job)

    # ── 取消 ──────────────────────────────────────────────────────────────

    def cancel_for_pr(self, repo: str, pr_number: int) -> None:
        """将该 PR 所有 pending job 标记 cancelled（不中断正在运行的）。"""
        with self._lock:
            self._cancel_for_pr_locked(repo, pr_number)

    def _cancel_for_pr_locked(self, repo: str, pr_number: int) -> None:
        for job in self._pending:
            if (
                job.repo == repo
                and job.pr_number == pr_number
                and job.status == "pending"
            ):
                job.status = "cancelled"

    # ── 取出 ──────────────────────────────────────────────────────────────

    def get_next_runnable(self, running_repos: set[str]) -> BuildJob | None:
        """
        从队列头部找第一个「repo 不在 running_repos 中」的 pending job，
        移出队列并返回；顺带清理队首 cancelled job；无则返回 None。
        """
        with self._lock:
            # 清理队首连续 cancelled
            while self._pending and self._pending[0].status == "cancelled":
                self._pending.popleft()

            for job in list(self._pending):
                if job.status == "pending" and job.repo not in running_repos:
                    self._pending.remove(job)
                    job.status = "running"
                    self._running.append(job)
                    return job
        return None

    # ── 完成 ──────────────────────────────────────────────────────────────

    def mark_done(self, job_id: str) -> None:
        with self._lock:
            self._running = [j for j in self._running if j.job_id != job_id]

    # ── 查询 ──────────────────────────────────────────────────────────────

    def running_repos(self) -> set[str]:
        with self._lock:
            return {j.repo for j in self._running}

    def queue_position(self, repo: str, pr_number: int) -> int:
        """返回该 PR 在 pending 列表中的位置（1-indexed）；不在队列返回 -1。"""
        with self._lock:
            pos = 0
            for job in self._pending:
                if job.status == "pending":
                    pos += 1
                    if job.repo == repo and job.pr_number == pr_number:
                        return pos
        return -1

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._pending if j.status == "pending")

    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    def all_pending_jobs(self) -> list[BuildJob]:
        """返回当前所有 pending / running job 的快照（用于 watchdog）。"""
        with self._lock:
            return [
                j for j in list(self._pending) + self._running
                if j.status in ("pending", "running")
            ]

    def has_active_job(self, repo: str, pr_number: int, head_sha: str) -> bool:
        """
        是否已有同一 repo + PR + head_sha 的任务在排队或执行中。
        用于避免构建尚未完成、持久化 state 未更新时，轮询线程重复入队并刷屏「发现新任务」。
        """
        with self._lock:
            for job in list(self._pending) + self._running:
                if job.status not in ("pending", "running"):
                    continue
                if (
                    job.repo == repo
                    and job.pr_number == pr_number
                    and job.head_sha == head_sha
                ):
                    return True
        return False
