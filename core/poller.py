"""PR 发现层：定时轮询 GitHub API，将新/更新的 PR 投入任务队列。"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import requests

from core import github_api
from core.queue import BuildJob, JobQueue

if TYPE_CHECKING:
    from core.reporter import Reporter

_SKIP_CI_MARKERS = ("[skip ci]", "[ci skip]", "[no ci]", "[skip-ci]")


class Poller:
    """
    逐仓库调用 GitHub list_open_prs（带 ETag），比较本地 pr_state，
    将「新 PR」或「head SHA 已更新的 PR」投入 JobQueue。

    若同一 (repo, PR, sha) 已在队列中 pending/running，则不再入队（避免构建期间重复「发现新任务」）。

    跳过规则（按优先级）：
      1. draft PR
      2. 标签含 "skip-ci" 或 "ci-skip"
      3. commit message 含 [skip ci] 等标记
      4. 作者不在 allowed_authors 白名单（白名单为空时不限制）
    """

    def __init__(
        self,
        watch_repos: list[str],
        target_branch: str,
        allowed_authors: list[str],
    ) -> None:
        self._watch_repos = watch_repos
        self._target_branch = target_branch
        self._allowed_authors = allowed_authors

    def poll_once(
        self,
        state: dict[tuple[str, int], str],
        queue: JobQueue,
        reporter: "Reporter",
    ) -> None:
        for repo in self._watch_repos:
            try:
                prs = github_api.list_open_prs(repo, self._target_branch)
            except requests.RequestException as e:
                print(f"[Warn] 轮询 {repo} 失败，跳过本轮: {e}", file=sys.stderr, flush=True)
                continue

            if prs is None:
                # 304 Not Modified，无变化
                continue

            for pr in prs:
                number = int(pr["number"])
                head_sha = str(pr["head_sha"])

                # ── 跳过规则 ──────────────────────────────────────────────
                if pr.get("draft"):
                    continue

                labels: list[str] = pr.get("labels", [])  # type: ignore[assignment]
                if "skip-ci" in labels or "ci-skip" in labels:
                    continue

                commit_msg = str(pr.get("commit_message", "")).lower()
                if any(m in commit_msg for m in _SKIP_CI_MARKERS):
                    continue

                author = str(pr.get("author", ""))
                if self._allowed_authors and author not in self._allowed_authors:
                    print(
                        f"[Info] 跳过 {repo} PR #{number}：作者 {author!r} 不在白名单",
                        flush=True,
                    )
                    continue

                # ── 与本地状态比较 ─────────────────────────────────────────
                key = (repo, number)
                if state.get(key) == head_sha:
                    continue  # 已持久化记录，跳过

                # 构建进行中时尚未写回 .pr_state.json，避免重复入队与重复 API 上报
                if queue.has_active_job(repo, number, head_sha):
                    continue

                print(
                    f"[Info] 发现新任务：{repo} PR #{number} sha={head_sha[:8]}",
                    flush=True,
                )
                job = BuildJob(repo=repo, pr_number=number, head_sha=head_sha)
                queue.enqueue(job)

                # 入队后立刻上报：设 Commit Status pending + 发 PR 评论
                pos = queue.queue_position(repo, number)
                try:
                    reporter.on_enqueue(job, pos)
                except requests.RequestException as e:
                    print(f"[Warn] 入队上报失败: {e}", file=sys.stderr, flush=True)
