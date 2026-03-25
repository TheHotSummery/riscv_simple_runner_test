"""主轮询循环。"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

import requests
from dotenv import load_dotenv

from core.config import load_config
from core import executor
from core import git_manager
from core import github_api

PR_STATE_FILE = ".pr_state.json"

running = True


def _handle_stop(_signum: int, _frame: object | None) -> None:
    global running
    print("[Info] 收到退出信号，准备安全退出...", flush=True)
    running = False


def _pr_state_path() -> str:
    return os.path.join(os.getcwd(), PR_STATE_FILE)


def read_pr_state() -> dict[int, str]:
    path = _pr_state_path()
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
    out: dict[int, str] = {}
    for k, v in data.items():
        try:
            num = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, str) and v:
            out[num] = v
    return out


def write_pr_state(state: dict[int, str]) -> None:
    data = {str(k): v for k, v in sorted(state.items(), key=lambda x: x[0])}
    with open(_pr_state_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)


def _sleep_interruptible(seconds: float) -> None:
    """可被 running=False 打断的睡眠（约 1s 粒度）。"""
    deadline = time.monotonic() + max(0.0, seconds)
    while running and time.monotonic() < deadline:
        time.sleep(min(1.0, deadline - time.monotonic()))


def _pick_next_pr() -> tuple[int, str] | None:
    prs = github_api.list_open_prs()
    if not prs:
        return None
    state = read_pr_state()
    for pr in prs:
        number = pr["number"]
        head_sha = pr["head_sha"]
        draft = pr.get("draft", False)
        if not isinstance(number, int) or not isinstance(head_sha, str):
            continue
        if bool(draft):
            continue
        last_sha = state.get(number)
        if last_sha != head_sha:
            return number, head_sha
    return None


def _tick() -> None:
    # PR 轮询模式：按创建时间升序，逐个处理未构建或更新过的 PR。
    next_pr = _pick_next_pr()
    if next_pr is None:
        return
    pr_number, sha = next_pr
    print(f"[Info] 处理 PR #{pr_number} head={sha}", flush=True)

    github_api.create_commit_status(sha)

    cfg = load_config()
    try:
        git_manager.sync_to_sha(
            cfg.workspace_dir,
            cfg.github_repo,
            cfg.github_token,
            sha,
        )
    except (OSError, RuntimeError) as e:
        log = f"Git 同步失败: {e}\n"
        try:
            github_api.update_commit_status(sha, "failure")
        except requests.RequestException as ex:
            print(f"更新 Commit Status 失败: {ex}", file=sys.stderr)
        return

    def _progress_cb(event: str, idx: int, total: int, step_name: str) -> None:
        if event == "start":
            desc = f"Running {idx}/{total}: {step_name}"
        elif event == "done":
            desc = f"Finished {idx}/{total}: {step_name}"
        else:
            desc = f"Step {idx}/{total}: {step_name}"
        print(f"[Info] {desc}", flush=True)
        try:
            github_api.update_commit_status_pending(sha, desc)
        except requests.RequestException as e:
            print(f"更新 Commit Status 失败: {e}", file=sys.stderr)

    conclusion, _log_text = executor.run_workflow(
        cfg.workspace_dir,
        cfg.step_timeout,
        _progress_cb,
    )
    try:
        github_api.update_commit_status(sha, conclusion)
    except requests.RequestException as e:
        print(f"更新 Commit Status 失败: {e}", file=sys.stderr)

    state = read_pr_state()
    state[pr_number] = sha
    write_pr_state(state)


def main() -> None:
    load_dotenv()
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    cfg = load_config()
    print(
        f"Runner 启动：repo={cfg.github_repo} branch={cfg.target_branch} "
        f"interval={cfg.poll_interval}s workspace={cfg.workspace_dir} "
        f"step_timeout={cfg.step_timeout}s",
        flush=True,
    )

    net_fail_streak = 0
    while running:
        try:
            _tick()
            net_fail_streak = 0
        except requests.RequestException as e:
            net_fail_streak += 1
            delay = min(cfg.poll_interval * (2**net_fail_streak), 300)
            print(
                f"网络/API 错误: {e}，{delay}s 后重试（连续失败 {net_fail_streak}）",
                file=sys.stderr,
            )
        except OSError as e:
            net_fail_streak = 0
            print(f"文件 I/O 错误: {e}", file=sys.stderr)
        except RuntimeError as e:
            net_fail_streak = 0
            print(f"配置或数据错误: {e}", file=sys.stderr)

        if not running:
            break

        if net_fail_streak > 0:
            _sleep_interruptible(delay)
        else:
            _sleep_interruptible(float(cfg.poll_interval))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已退出。", file=sys.stderr)
        sys.exit(130)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
