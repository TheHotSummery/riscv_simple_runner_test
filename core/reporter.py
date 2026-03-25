"""构建状态上报：Commit Status + PR 评论（入队 / 开始 / 完成 / 离线）。"""

from __future__ import annotations

import os
import sys

import requests

from core import github_api
from core.queue import BuildJob

_LOG_TAIL_LINES = 40


def write_log(log_dir: str, job_id: str, log_text: str) -> str:
    """将构建日志写入 {log_dir}/{job_id}.log，返回绝对路径。"""
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{job_id}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(log_text)
    return path


def _read_log_tail(log_path: str, lines: int = _LOG_TAIL_LINES) -> str:
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return "".join(tail).rstrip()
    except OSError:
        return "(日志文件不可读)"


class Reporter:
    """
    集中管理所有「写回 GitHub」的操作：
      - Commit Status（pending / success / failure）
      - PR 评论（入队 → 开始 → 完成，复用同一条 comment_id）

    每次操作独立 try/except，单个 API 失败不影响整体流程。
    """

    def __init__(
        self,
        post_pr_comment: bool,
        runner_board: str,
        log_dir: str,
    ) -> None:
        self._post = post_pr_comment
        self._board = runner_board
        self._log_dir = log_dir

    # ── 入队 ──────────────────────────────────────────────────────────────

    def on_enqueue(self, job: BuildJob, queue_pos: int) -> None:
        """设 Commit Status=pending（queued）；若启用评论则新建 PR 评论并保存 comment_id。"""
        try:
            github_api.create_commit_status(job.repo, job.head_sha)
        except requests.RequestException as e:
            print(f"[Warn] create_commit_status 失败: {e}", file=sys.stderr, flush=True)

        if not self._post:
            return

        pending_before = max(0, queue_pos - 1)
        wait_hint = f"（前方有 {pending_before} 个任务）" if pending_before > 0 else ""
        body = (
            f"⏳ **RISC-V CI 已入队**\n"
            f"- 队列位置：**#{queue_pos}**{wait_hint}\n"
            f"- 机器：`{self._board}`\n"
            f"- 触发 SHA：`{job.head_sha[:8]}`\n"
        )
        try:
            comment_id = github_api.create_pr_comment(job.repo, job.pr_number, body)
            job.comment_id = comment_id
        except requests.RequestException as e:
            print(f"[Warn] 创建入队评论失败: {e}", file=sys.stderr, flush=True)

    # ── 构建开始 ──────────────────────────────────────────────────────────

    def on_start(self, job: BuildJob) -> None:
        """更新 Commit Status 为 building；编辑 PR 评论为「构建中」状态。"""
        try:
            github_api.update_commit_status_pending(
                job.repo, job.head_sha,
                f"Building on {self._board}.",
            )
        except requests.RequestException as e:
            print(f"[Warn] update_commit_status_pending 失败: {e}", file=sys.stderr, flush=True)

        if not self._post:
            return

        body = (
            f"🔨 **RISC-V CI 构建中**\n"
            f"- 机器：`{self._board}`\n"
            f"- SHA：`{job.head_sha[:8]}`\n"
        )
        self._edit_or_create(job, body)

    # ── 步骤进度 ──────────────────────────────────────────────────────────

    def on_step_progress(self, job: BuildJob, desc: str) -> None:
        """每步开始/完成时更新 Commit Status 描述（不刷 PR 评论，避免频繁 API 调用）。"""
        try:
            github_api.update_commit_status_pending(job.repo, job.head_sha, desc)
        except requests.RequestException as e:
            print(f"[Warn] 更新步骤进度失败: {e}", file=sys.stderr, flush=True)

    # ── 构建完成 ──────────────────────────────────────────────────────────

    def on_done(
        self,
        job: BuildJob,
        conclusion: str,
        log_text: str,
        elapsed_sec: float,
    ) -> None:
        """写日志文件、更新最终 Commit Status、编辑 PR 评论为完成状态（含日志摘要）。"""
        log_path = write_log(self._log_dir, job.job_id, log_text)

        desc = (
            f"Build {'succeeded' if conclusion == 'success' else 'failed'} "
            f"on {self._board}, {elapsed_sec:.0f}s"
        )
        try:
            github_api.update_commit_status(job.repo, job.head_sha, conclusion, desc)
        except requests.RequestException as e:
            print(f"[Warn] update_commit_status 失败: {e}", file=sys.stderr, flush=True)

        if not self._post:
            return

        icon = "✅" if conclusion == "success" else "❌"
        result_word = "构建成功" if conclusion == "success" else "构建失败"
        log_tail = _read_log_tail(log_path)
        body = (
            f"{icon} **RISC-V CI {result_word}**（耗时 {elapsed_sec:.0f}s）\n"
            f"- 机器：`{self._board}`\n"
            f"- SHA：`{job.head_sha[:8]}`\n"
            f"- 完整日志：`{log_path}`\n\n"
            f"<details><summary>日志末尾（最后 {_LOG_TAIL_LINES} 行）</summary>\n\n"
            f"```\n{log_tail}\n```\n\n</details>\n"
        )
        self._edit_or_create(job, body)

    # ── Git/repo 同步失败 ──────────────────────────────────────────────────

    def on_sync_failure(self, job: BuildJob, error: str) -> None:
        """工作区同步失败时：标记 failure + 更新评论。"""
        desc = f"Git sync failed on {self._board}."
        try:
            github_api.update_commit_status(job.repo, job.head_sha, "failure", desc)
        except requests.RequestException as e:
            print(f"[Warn] update_commit_status 失败: {e}", file=sys.stderr, flush=True)

        if not self._post:
            return

        body = (
            f"❌ **RISC-V CI Git 同步失败**\n"
            f"- 机器：`{self._board}`\n"
            f"- 错误：`{error[:300]}`\n"
        )
        self._edit_or_create(job, body)

    # ── Runner 离线告警（Watchdog 调用）──────────────────────────────────

    def on_runner_offline(self, pending_jobs: list[BuildJob]) -> None:
        """Watchdog 检测超时时调用，将所有孤立 pending 状态置为 failure 并发警告评论。"""
        for job in pending_jobs:
            try:
                github_api.update_commit_status(
                    job.repo, job.head_sha, "failure",
                    f"Runner {self._board} offline.",
                )
            except requests.RequestException:
                pass

            if self._post and job.comment_id is not None:
                body = (
                    f"⚠️ **RISC-V CI Runner 离线**\n"
                    f"- 机器 `{self._board}` 疑似已离线，CI 无法继续。\n"
                    f"- 请联系管理员重启 Runner，或重新 push 触发构建。\n"
                )
                try:
                    github_api.edit_pr_comment(job.repo, job.comment_id, body)
                except requests.RequestException:
                    pass

    # ── 工具 ──────────────────────────────────────────────────────────────

    def _edit_or_create(self, job: BuildJob, body: str) -> None:
        """若已有 comment_id 则编辑，否则新建评论。"""
        try:
            if job.comment_id is not None:
                github_api.edit_pr_comment(job.repo, job.comment_id, body)
            else:
                comment_id = github_api.create_pr_comment(job.repo, job.pr_number, body)
                job.comment_id = comment_id
        except requests.RequestException as e:
            print(f"[Warn] PR 评论操作失败: {e}", file=sys.stderr, flush=True)
