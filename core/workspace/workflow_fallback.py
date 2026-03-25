"""PR 分支缺少 .riscv/workflow.yml 时，从 origin 目标分支单独检出该文件。"""

from __future__ import annotations

import os
import subprocess


WORKFLOW_GIT_PATH = ".riscv/workflow.yml"


def try_materialize_workflow_from_target_branch(
    git_cwd: str,
    target_branch: str,
) -> bool:
    """
    在已有 git 仓库目录 git_cwd 内，从 origin/<target_branch> 检出 .riscv/workflow.yml。
    其余文件保持当前 HEAD（例如 PR 的 head_sha）不变。

    成功返回 True；fetch/checkout 失败或目标分支无该文件返回 False。
    """
    fetch = subprocess.run(
        ["git", "-C", git_cwd, "fetch", "origin", target_branch],
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        return False

    co = subprocess.run(
        [
            "git",
            "-C",
            git_cwd,
            "checkout",
            f"origin/{target_branch}",
            "--",
            WORKFLOW_GIT_PATH,
        ],
        capture_output=True,
        text=True,
    )
    if co.returncode != 0:
        return False

    return os.path.isfile(os.path.join(git_cwd, WORKFLOW_GIT_PATH))
