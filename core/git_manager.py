"""将仓库同步到指定 SHA（clone 或 fetch + reset）。"""

from __future__ import annotations

import os
import subprocess
from urllib.parse import quote


def _authed_clone_url(repo: str, token: str) -> str:
    safe = quote(token, safe="")
    return f"https://x-access-token:{safe}@github.com/{repo}.git"


def sync_to_sha(workspace_dir: str, repo: str, token: str, sha: str) -> None:
    """
    若 workspace_dir/.git 不存在则 clone；否则 fetch origin 后 reset --hard 到 sha。
    """
    git_meta = os.path.join(workspace_dir, ".git")
    url = _authed_clone_url(repo, token)

    if not os.path.isdir(git_meta):
        parent = os.path.dirname(os.path.abspath(workspace_dir))
        if parent:
            os.makedirs(parent, exist_ok=True)
        r = subprocess.run(
            ["git", "clone", url, workspace_dir],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(f"git clone 失败 (exit {r.returncode}): {err}")

    fetch = subprocess.run(
        ["git", "-C", workspace_dir, "fetch", "origin"],
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        err = (fetch.stderr or fetch.stdout or "").strip()
        raise RuntimeError(f"git fetch 失败 (exit {fetch.returncode}): {err}")

    reset = subprocess.run(
        ["git", "-C", workspace_dir, "reset", "--hard", sha],
        capture_output=True,
        text=True,
    )
    if reset.returncode != 0:
        err = (reset.stderr or reset.stdout or "").strip()
        raise RuntimeError(f"git reset --hard 失败 (exit {reset.returncode}): {err}")
