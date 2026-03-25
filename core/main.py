"""主调度器：轮询线程 + Worker 线程组 + Watchdog 心跳。"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from typing import Any

import requests
from dotenv import load_dotenv

from core.config import load_config
from core import executor
from core.poller import Poller
from core.queue import BuildJob, JobQueue
from core.reporter import Reporter
from core.workspace import PRInfo, WorkspaceBase
from core.workspace.git_ws import GitWorkspace
from core.workspace.repo_ws import RepoWorkspace

PR_STATE_FILE = ".pr_state.json"
HEARTBEAT_FILE = ".runner_heartbeat"
HEARTBEAT_INTERVAL_SEC = 30   # watchdog 写心跳的频率

running = True
_state_lock = threading.Lock()


# ── 信号处理 ──────────────────────────────────────────────────────────────────

def _handle_stop(_signum: int, _frame: Any) -> None:
    global running
    print("[Info] 收到退出信号，准备安全退出...", flush=True)
    running = False


# ── PR 状态持久化 ─────────────────────────────────────────────────────────────

def _state_path() -> str:
    return os.path.join(os.getcwd(), PR_STATE_FILE)


def read_pr_state() -> dict[tuple[str, int], str]:
    """
    返回 {(repo, pr_number): head_sha}。
    支持新格式 "org/repo#42" 和旧格式 "42"（旧格式直接丢弃，让 PR 重新触发一次）。
    """
    path = _state_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return {}
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}

    out: dict[tuple[str, int], str] = {}
    for k, v in data.items():
        if not isinstance(v, str) or not v:
            continue
        if "#" in k:
            repo, num_str = k.rsplit("#", 1)
            try:
                out[(repo, int(num_str))] = v
            except ValueError:
                pass
        # 旧格式（只有数字）直接跳过，让 PR 重跑一次以迁移状态
    return out


def write_pr_state(state: dict[tuple[str, int], str]) -> None:
    data = {
        f"{repo}#{num}": sha
        for (repo, num), sha in sorted(state.items(), key=lambda x: (x[0][0], x[0][1]))
    }
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)


# ── 心跳 ──────────────────────────────────────────────────────────────────────

def _touch_heartbeat() -> None:
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


# ── 工具 ──────────────────────────────────────────────────────────────────────

def _sleep_interruptible(seconds: float) -> None:
    """可被 running=False 打断的睡眠（约 1s 粒度）。"""
    deadline = time.monotonic() + max(0.0, seconds)
    while running and time.monotonic() < deadline:
        time.sleep(min(1.0, deadline - time.monotonic()))


def make_workspace(cfg: Any) -> WorkspaceBase:
    if cfg.workspace_mode == "repo":
        if not cfg.manifest_repo:
            raise RuntimeError("WORKSPACE_MODE=repo 时必须设置 MANIFEST_REPO")
        return RepoWorkspace(
            workspace_dir=cfg.workspace_dir,
            manifest_repo=cfg.manifest_repo,
            manifest_branch=cfg.manifest_branch,
            manifest_file=cfg.manifest_file,
            token=cfg.github_token,
            manifest_github_org=cfg.manifest_github_org,
        )
    return GitWorkspace(
        workspace_dir=cfg.workspace_dir,
        repo=cfg.github_repo,
        token=cfg.github_token,
    )


# ── Worker 线程 ───────────────────────────────────────────────────────────────

def _worker_loop(
    queue: JobQueue,
    workspace: WorkspaceBase,
    reporter: Reporter,
    state: dict[tuple[str, int], str],
    cfg: Any,
) -> None:
    while running:
        running_repos = queue.running_repos()
        job = queue.get_next_runnable(running_repos)
        if job is None:
            time.sleep(1)
            continue

        print(
            f"[Info] 开始处理 {job.repo} PR #{job.pr_number} sha={job.head_sha[:8]}",
            flush=True,
        )
        start_time = time.monotonic()
        reporter.on_start(job)

        # 1. 工作区同步
        try:
            workspace.sync_for_pr(PRInfo(
                repo=job.repo,
                pr_number=job.pr_number,
                head_sha=job.head_sha,
            ))
        except (OSError, RuntimeError) as e:
            err_msg = str(e)
            print(
                f"[Error] 工作区同步失败 PR #{job.pr_number}: {err_msg}",
                file=sys.stderr, flush=True,
            )
            reporter.on_sync_failure(job, err_msg)
            queue.mark_done(job.job_id)
            continue

        workspace.clean_artifacts()

        # 2. 执行工作流
        def _progress_cb(event: str, idx: int, total: int, step_name: str) -> None:
            if event == "start":
                desc = f"Running {idx}/{total}: {step_name} on {cfg.runner_board}"
            elif event == "done":
                desc = f"Finished {idx}/{total}: {step_name}"
            else:
                desc = f"Step {idx}/{total}: {step_name}"
            reporter.on_step_progress(job, desc)

        conclusion, log_text = executor.run_workflow(
            workspace.workflow_dir(),
            cfg.step_timeout,
            _progress_cb,
        )
        elapsed = time.monotonic() - start_time

        # 3. 上报结果
        reporter.on_done(job, conclusion, log_text, elapsed)
        print(
            f"[Info] {job.repo} PR #{job.pr_number} 完成：{conclusion}，耗时 {elapsed:.0f}s",
            flush=True,
        )

        queue.mark_done(job.job_id)

        with _state_lock:
            state[(job.repo, job.pr_number)] = job.head_sha
            write_pr_state(state)


# ── Watchdog 线程 ─────────────────────────────────────────────────────────────

def _watchdog_loop(queue: JobQueue, reporter: Reporter) -> None:
    """
    定期写心跳文件；Runner 意外死掉后外部监控可读此文件检测离线。
    当前进程内的 watchdog 只负责写心跳，不做自动离线处理
    （systemd/supervisord 负责重启，重启后状态会由新进程处理）。
    """
    while running:
        _touch_heartbeat()
        _sleep_interruptible(HEARTBEAT_INTERVAL_SEC)


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    cfg = load_config()
    print(
        f"Runner 启动：mode={cfg.workspace_mode} board={cfg.runner_board} "
        f"interval={cfg.poll_interval}s workspace={cfg.workspace_dir} "
        f"max_parallel={cfg.max_parallel_jobs} cancel_superseded={cfg.cancel_superseded}",
        flush=True,
    )

    # repo 模式暂不支持真正并行（共享工作区），自动降为 1
    max_parallel = cfg.max_parallel_jobs
    if cfg.workspace_mode == "repo" and max_parallel > 1:
        print(
            "[Warn] WORKSPACE_MODE=repo 暂不支持 MAX_PARALLEL_JOBS > 1，已自动降为 1",
            flush=True,
        )
        max_parallel = 1

    # 初始化工作区
    workspace = make_workspace(cfg)
    print("[Info] 初始化工作区...", flush=True)
    workspace.bootstrap()
    print("[Info] 工作区就绪。", flush=True)

    # repo 模式：若 watch_repos 为空则从 manifest 自动发现
    watch_repos = list(cfg.watch_repos)
    if cfg.workspace_mode == "repo" and not watch_repos:
        if isinstance(workspace, RepoWorkspace):
            watch_repos = workspace.discover_watch_repos()
            print(
                f"[Info] 自动发现 {len(watch_repos)} 个子仓库: {watch_repos}",
                flush=True,
            )

    if not watch_repos:
        raise RuntimeError(
            "没有可监听的仓库：git 模式请设置 GITHUB_REPO，"
            "repo 模式请设置 WATCH_REPOS 或确保 manifest 可解析"
        )

    state = read_pr_state()
    queue = JobQueue(cancel_superseded=cfg.cancel_superseded)
    reporter = Reporter(
        post_pr_comment=cfg.post_pr_comment,
        runner_board=cfg.runner_board,
        log_dir=cfg.log_dir,
    )
    poller = Poller(
        watch_repos=watch_repos,
        target_branch=cfg.target_branch,
        allowed_authors=list(cfg.allowed_authors),
    )

    # 启动 Worker 线程
    for i in range(max_parallel):
        t = threading.Thread(
            target=_worker_loop,
            args=(queue, workspace, reporter, state, cfg),
            name=f"worker-{i}",
            daemon=True,
        )
        t.start()

    # 启动 Watchdog 线程
    threading.Thread(
        target=_watchdog_loop,
        args=(queue, reporter),
        name="watchdog",
        daemon=True,
    ).start()

    # 主线程：轮询循环
    net_fail_streak = 0
    last_idle_log = time.monotonic()
    while running:
        try:
            poller.poll_once(state, queue, reporter)
            net_fail_streak = 0
        except requests.RequestException as e:
            net_fail_streak += 1
            delay = min(cfg.poll_interval * (2 ** net_fail_streak), 300)
            print(
                f"网络/API 错误: {e}，{delay:.0f}s 后重试（连续失败 {net_fail_streak}）",
                file=sys.stderr, flush=True,
            )
            _sleep_interruptible(delay)
            continue
        except OSError as e:
            print(f"文件 I/O 错误: {e}", file=sys.stderr, flush=True)
        except RuntimeError as e:
            print(f"运行时错误: {e}", file=sys.stderr, flush=True)

        if not running:
            break

        # 周期性空闲日志（避免日志静默太久）
        now = time.monotonic()
        if now - last_idle_log >= 60:
            pending = queue.pending_count()
            active = queue.running_count()
            print(
                f"[Info] 轮询中 pending={pending} running={active} "
                f"watching={len(watch_repos)} repos",
                flush=True,
            )
            last_idle_log = now

        _sleep_interruptible(float(cfg.poll_interval))

    print("[Info] Runner 已停止。", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已退出。", file=sys.stderr)
        sys.exit(130)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
