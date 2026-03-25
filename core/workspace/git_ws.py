"""单 Git 仓库工作区实现（原 git_manager.py 的重构版本）。"""

from __future__ import annotations

import os
import subprocess
from urllib.parse import quote

from core.config import normalize_github_repo_slug
from core.workspace import PRInfo, WorkspaceBase
from core.workspace.workflow_fallback import try_materialize_workflow_from_target_branch


def _authed_clone_url(repo: str, token: str) -> str:
    repo = normalize_github_repo_slug(repo)
    safe = quote(token, safe="")
    return f"https://x-access-token:{safe}@github.com/{repo}.git"


class GitWorkspace(WorkspaceBase):
    """
    单 Git 仓库工作区。

    bootstrap : 若 .git 不存在则 git clone，否则跳过。
    sync_for_pr : git fetch origin + git reset --hard head_sha。
    """

    def __init__(self, workspace_dir: str, repo: str, token: str) -> None:
        self._workspace_dir = workspace_dir
        self._repo = repo
        self._token = token

    # ── WorkspaceBase 实现 ────────────────────────────────────────────────

    def bootstrap(self) -> None:
        git_meta = os.path.join(self._workspace_dir, ".git")
        if os.path.isdir(git_meta):
            return  # 已存在，幂等

        parent = os.path.dirname(os.path.abspath(self._workspace_dir))
        if parent:
            os.makedirs(parent, exist_ok=True)

        url = _authed_clone_url(self._repo, self._token)
        r = subprocess.run(
            ["git", "clone", url, self._workspace_dir],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(f"git clone 失败 (exit {r.returncode}): {err}")

    def sync_for_pr(self, pr: PRInfo) -> None:
        # 若 workspace 尚未初始化（例如首次），先 bootstrap
        if not os.path.isdir(os.path.join(self._workspace_dir, ".git")):
            self.bootstrap()

        fetch = subprocess.run(
            ["git", "-C", self._workspace_dir, "fetch", "origin"],
            capture_output=True,
            text=True,
        )
        if fetch.returncode != 0:
            err = (fetch.stderr or fetch.stdout or "").strip()
            raise RuntimeError(f"git fetch 失败 (exit {fetch.returncode}): {err}")

        reset = subprocess.run(
            ["git", "-C", self._workspace_dir, "reset", "--hard", pr.head_sha],
            capture_output=True,
            text=True,
        )
        if reset.returncode != 0:
            err = (reset.stderr or reset.stdout or "").strip()
            raise RuntimeError(f"git reset --hard 失败 (exit {reset.returncode}): {err}")

    def workflow_dir(self) -> str:
        return self._workspace_dir

    def ensure_workflow_for_build(self, pr: PRInfo, target_branch: str) -> str:
        d = self.workflow_dir_for_pr(pr)
        wf = os.path.join(d, ".riscv", "workflow.yml")
        if os.path.isfile(wf):
            return d
        if try_materialize_workflow_from_target_branch(d, target_branch):
            if os.path.isfile(wf):
                print(
                    f"[Info] PR 分支缺少 .riscv/workflow.yml，已从 origin/{target_branch} 检出",
                    flush=True,
                )
                return d
        return d
