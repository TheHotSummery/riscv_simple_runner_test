"""环境变量加载与校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    github_token: str
    github_repo: str
    target_branch: str
    poll_interval: int
    workspace_dir: str
    step_timeout: int


def load_config() -> Config:
    required = ("GITHUB_TOKEN", "GITHUB_REPO", "TARGET_BRANCH")
    missing = [k for k in required if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError(f"缺少必需环境变量: {', '.join(missing)}")

    raw = os.environ.get("POLL_INTERVAL", "15").strip()
    try:
        poll = int(raw)
    except ValueError as e:
        raise RuntimeError("POLL_INTERVAL 必须是整数（秒）") from e
    if poll <= 0:
        raise RuntimeError("POLL_INTERVAL 必须为正整数")

    ws = os.environ.get("WORKSPACE_DIR", "./workspace").strip() or "./workspace"
    workspace_dir = os.path.abspath(os.path.expanduser(ws))

    raw_step = os.environ.get("STEP_TIMEOUT", "3600").strip()
    try:
        step_timeout = int(raw_step)
    except ValueError as e:
        raise RuntimeError("STEP_TIMEOUT 必须是整数（秒）") from e
    if step_timeout <= 0:
        raise RuntimeError("STEP_TIMEOUT 必须为正整数")

    return Config(
        github_token=os.environ["GITHUB_TOKEN"].strip(),
        github_repo=os.environ["GITHUB_REPO"].strip(),
        target_branch=os.environ["TARGET_BRANCH"].strip(),
        poll_interval=poll,
        workspace_dir=workspace_dir,
        step_timeout=step_timeout,
    )
