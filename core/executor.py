"""解析 riscv-ci.yml 并按顺序执行 steps（进程组 + 超时猎杀）。"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import yaml
from typing import Callable, Optional
import threading
import queue
import time

WORKFLOW_REL = os.path.join(".riscv", "workflow.yml")

# 匹配 shell 脚本中的 sudo（词边界，避免误伤不含独立 sudo 的字符串）
_SUDO_PATTERN = re.compile(r"\bsudo\b")


def _skip_sudo_steps_enabled() -> bool:
    v = os.environ.get("SKIP_SUDO_STEPS", "true").strip().lower()
    return v not in ("false", "0", "no", "off")


def _sudo_skip_log_block(run_cmd: str) -> str:
    return (
        "[Sudo] 检测到本步骤的 run 中包含 sudo。\n"
        "在无交互终端的自托管 Runner 上，sudo 通常无法输入密码而失败（A terminal is required to authenticate）。\n"
        "已根据环境变量 SKIP_SUDO_STEPS（默认启用）跳过本步骤的实际执行，本步骤计为成功。\n"
        "建议：在 Runner 机器上预装依赖，或为 CI 用户配置免密 sudo（见 DEPLOY.md）；勿在仓库中保存密码。\n"
        "若需强制执行含 sudo 的步骤，可设置 SKIP_SUDO_STEPS=false（风险自负）。\n"
        "若后续步骤依赖本步安装的软件包，可能会失败。\n"
        "—— 以下为未执行的命令 ——\n"
        f"{run_cmd}\n"
        "—— 结束 ——\n"
    )


def _merge_streams(stdout: str | None, stderr: str | None) -> str:
    out = stdout or ""
    err = stderr or ""
    merged = out
    if err:
        if merged and not merged.endswith("\n"):
            merged += "\n"
        merged += err
    return merged


def _kill_process_tree(proc: subprocess.Popen[str], sig: int) -> None:
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, sig)
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_step_shell(
    run_cmd: str,
    workspace_dir: str,
    step_timeout_sec: int,
) -> tuple[int, str]:
    """
    使用新会话（setsid）启动 shell，超时则 SIGTERM 整个进程组，必要时 SIGKILL。
    返回 (returncode, merged_log)。
    """
    proc = subprocess.Popen(
        run_cmd,
        shell=True,
        cwd=workspace_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        preexec_fn=os.setsid,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    q: "queue.Queue[tuple[str, str | object]]" = queue.Queue()
    sentinel = object()

    def _reader(name: str, pipe) -> None:
        try:
            for line in iter(pipe.readline, ""):
                q.put((name, line))
        finally:
            q.put((name, sentinel))

    threads = [
        threading.Thread(target=_reader, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=_reader, args=("stderr", proc.stderr), daemon=True),
    ]
    for t in threads:
        t.start()

    logs: list[str] = []
    done = 0
    timed_out = False
    deadline = time.monotonic() + max(0, step_timeout_sec)

    while True:
        if time.monotonic() > deadline:
            timed_out = True
            _kill_process_tree(proc, signal.SIGTERM)
            break

        try:
            name, item = q.get(timeout=0.2)
        except queue.Empty:
            name, item = "", None

        if item is sentinel:
            done += 1
        elif isinstance(item, str):
            logs.append(item)
            print(item, end="", flush=True)

        if proc.poll() is not None and done >= 2 and q.empty():
            break

    if timed_out:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc, signal.SIGKILL)
            proc.wait()
        return -1, "".join(logs)

    return proc.returncode, "".join(logs)


def run_workflow(
    workspace_dir: str,
    step_timeout_sec: int,
    progress_cb: Optional[Callable[[str, int, int, str], None]] = None,
) -> tuple[str, str]:
    """
    读取 workspace 内 .riscv/workflow.yml，顺序执行 steps。
    若某步 run 中含 sudo 且 SKIP_SUDO_STEPS 为真（默认），则跳过该步执行并写入说明日志，该步视为成功。
    返回 (conclusion, total_log_text)，conclusion 为 success 或 failure。
    """
    path = os.path.join(workspace_dir, WORKFLOW_REL)
    if not os.path.isfile(path):
        return "failure", f"未找到工作流文件: {path}"

    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return "failure", f"YAML 解析失败: {e}"
    except OSError as e:
        return "failure", f"无法读取工作流: {e}"

    if not isinstance(doc, dict):
        return "failure", "工作流根节点必须是映射（键值对）。"

    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        return "failure", "工作流必须包含非空的 steps 列表。"

    logs: list[str] = []

    total_steps = len(steps)
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            logs.append(f"\n=== Step {i + 1}: <invalid> ===\n步骤定义必须是映射。\n")
            return "failure", "".join(logs)

        name = raw.get("name", f"step-{i + 1}")
        run_cmd = raw.get("run")
        step_name = name if isinstance(name, str) else str(name)
        logs.append(f"\n=== Step: {step_name} ===\n")
        if progress_cb is not None:
            progress_cb("start", i + 1, total_steps, step_name)

        if not isinstance(run_cmd, str) or not run_cmd.strip():
            logs.append("错误: 缺少有效的 run 字段。\n")
            return "failure", "".join(logs)

        if _skip_sudo_steps_enabled() and _SUDO_PATTERN.search(run_cmd):
            skip_msg = _sudo_skip_log_block(run_cmd)
            logs.append(skip_msg)
            print(skip_msg, flush=True)
            if progress_cb is not None:
                progress_cb("done", i + 1, total_steps, step_name)
            continue

        try:
            code, merged = _run_step_shell(run_cmd, workspace_dir, step_timeout_sec)
        except OSError as e:
            logs.append(f"无法执行命令: {e}\n")
            return "failure", "".join(logs)

        logs.append(merged)
        if not logs[-1].endswith("\n"):
            logs.append("\n")

        if code == -1:
            logs.append(
                f"\n[Error] 步骤执行超时 ({step_timeout_sec}s)，已强制终止进程树。\n"
            )
            return "failure", "".join(logs)

        if code != 0:
            logs.append(f"\n步骤以退出码 {code} 结束，中止后续步骤。\n")
            return "failure", "".join(logs)

        if progress_cb is not None:
            progress_cb("done", i + 1, total_steps, step_name)

    return "success", "".join(logs)
