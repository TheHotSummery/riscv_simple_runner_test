"""环境变量加载与校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # ── 必填 ────────────────────────────────────────────────────────────────
    github_token: str
    target_branch: str

    # ── 工作区模式 ──────────────────────────────────────────────────────────
    workspace_mode: str   # "git" | "repo"
    workspace_dir: str

    # git 模式专有（workspace_mode="git" 时必填）
    github_repo: str

    # repo 模式专有（workspace_mode="repo" 时必填）
    manifest_repo: str
    manifest_branch: str
    manifest_file: str
    # 可选：GitHub 组织/用户名；manifest 里 remote fetch 为相对路径时，
    # 用于拼出 owner/repo（若留空则从 fetch 路径自动推断，如 ../spacemit-robotics）
    manifest_github_org: str

    # 监听的仓库列表：git 模式自动填充为 [github_repo]；
    # repo 模式若留空则从 manifest 自动解析
    watch_repos: tuple[str, ...]

    # ── 调度 ────────────────────────────────────────────────────────────────
    poll_interval: int         # 轮询间隔（秒）
    step_timeout: int          # 单步超时（秒）
    max_parallel_jobs: int     # 最大并行 job 数
    cancel_superseded: bool    # 同 PR 新 push 时取消旧 pending job

    # ── 上报 ────────────────────────────────────────────────────────────────
    post_pr_comment: bool      # 是否在 PR 贴/编辑评论
    log_dir: str               # 构建日志存储目录
    runner_board: str          # 机器标识（显示在状态描述里）

    # ── 工作流文件目录（最高优先级）────────────────────────────────────────
    # 留空 = 按默认逻辑查找（子仓库 → base 分支检出 → 工作区根）
    # 设置后：所有 PR 一律用此目录下的 .riscv/workflow.yml，cwd 也为该目录
    # 典型用途：指向 repo 工作区内的 build/ 目录（统一构建入口），
    #           例如 WORKFLOW_DIR=./workspace/build
    workflow_dir_override: str

    # ── 安全 ────────────────────────────────────────────────────────────────
    # 空元组 = 不限制，允许所有 PR 作者触发构建
    allowed_authors: tuple[str, ...]


def normalize_github_repo_slug(s: str) -> str:
    """
    将 GitHub 仓库名规范为 owner/repo 形式（不含 .git 后缀）。
    若 .env 中误写 MANIFEST_REPO=org/foo.git，与代码里拼接的 .git 会叠成 foo.git.git 导致 clone 失败。
    """
    r = s.strip()
    while len(r) > 4 and r.lower().endswith(".git"):
        r = r[:-4].rstrip()
    return r


def _parse_bool(val: str, default: bool) -> bool:
    if not val:
        return default
    return val.lower() not in ("false", "0", "no", "off")


def _parse_int(val: str, name: str) -> int:
    try:
        return int(val)
    except ValueError as e:
        raise RuntimeError(f"{name} 必须是整数") from e


def load_config() -> Config:
    required = ("GITHUB_TOKEN", "TARGET_BRANCH")
    missing = [k for k in required if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError(f"缺少必需环境变量: {', '.join(missing)}")

    workspace_mode = os.environ.get("WORKSPACE_MODE", "git").strip().lower()
    if workspace_mode not in ("git", "repo"):
        raise RuntimeError('WORKSPACE_MODE 只能是 "git" 或 "repo"')

    github_repo = normalize_github_repo_slug(os.environ.get("GITHUB_REPO", ""))
    if workspace_mode == "git" and not github_repo:
        raise RuntimeError("WORKSPACE_MODE=git 时必须设置 GITHUB_REPO")

    ws = os.environ.get("WORKSPACE_DIR", "./workspace").strip() or "./workspace"
    workspace_dir = os.path.abspath(os.path.expanduser(ws))

    poll = _parse_int(os.environ.get("POLL_INTERVAL", "15").strip() or "15", "POLL_INTERVAL")
    if poll <= 0:
        raise RuntimeError("POLL_INTERVAL 必须为正整数")

    step_timeout = _parse_int(
        os.environ.get("STEP_TIMEOUT", "3600").strip() or "3600", "STEP_TIMEOUT"
    )
    if step_timeout <= 0:
        raise RuntimeError("STEP_TIMEOUT 必须为正整数")

    max_parallel = _parse_int(
        os.environ.get("MAX_PARALLEL_JOBS", "1").strip() or "1", "MAX_PARALLEL_JOBS"
    )
    if max_parallel <= 0:
        raise RuntimeError("MAX_PARALLEL_JOBS 必须为正整数")

    # watch_repos：git 模式自动设为 [github_repo]，repo 模式可手动指定或留空
    raw_watch = os.environ.get("WATCH_REPOS", "").strip()
    if raw_watch:
        watch_repos = tuple(
            normalize_github_repo_slug(r)
            for r in raw_watch.split(",")
            if r.strip()
        )
    elif workspace_mode == "git" and github_repo:
        watch_repos = (github_repo,)
    else:
        watch_repos = ()  # repo 模式下留空 → bootstrap 后自动解析

    raw_authors = os.environ.get("ALLOWED_AUTHORS", "").strip()
    allowed_authors: tuple[str, ...] = (
        tuple(a.strip() for a in raw_authors.split(",") if a.strip())
        if raw_authors
        else ()
    )

    log_dir_raw = os.environ.get("LOG_DIR", "./logs").strip() or "./logs"
    log_dir = os.path.abspath(os.path.expanduser(log_dir_raw))

    wf_override_raw = os.environ.get("WORKFLOW_DIR", "").strip()
    workflow_dir_override = (
        os.path.abspath(os.path.expanduser(wf_override_raw))
        if wf_override_raw
        else ""
    )

    return Config(
        github_token=os.environ["GITHUB_TOKEN"].strip(),
        target_branch=os.environ["TARGET_BRANCH"].strip(),
        workspace_mode=workspace_mode,
        workspace_dir=workspace_dir,
        github_repo=github_repo,
        manifest_repo=normalize_github_repo_slug(
            os.environ.get("MANIFEST_REPO", "").strip()
        ),
        manifest_branch=os.environ.get("MANIFEST_BRANCH", "main").strip() or "main",
        manifest_file=os.environ.get("MANIFEST_FILE", "default.xml").strip() or "default.xml",
        manifest_github_org=os.environ.get("MANIFEST_GITHUB_ORG", "").strip(),
        watch_repos=watch_repos,
        poll_interval=poll,
        step_timeout=step_timeout,
        max_parallel_jobs=max_parallel,
        cancel_superseded=_parse_bool(
            os.environ.get("CANCEL_SUPERSEDED", "").strip(), default=True
        ),
        post_pr_comment=_parse_bool(
            os.environ.get("POST_PR_COMMENT", "").strip(), default=True
        ),
        log_dir=log_dir,
        runner_board=os.environ.get("RUNNER_BOARD", "deb1").strip() or "deb1",
        allowed_authors=allowed_authors,
        workflow_dir_override=workflow_dir_override,
    )
