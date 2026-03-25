"""解析 riscv-ci.yml 并按顺序执行 steps（进程组 + 超时猎杀）。"""

from __future__ import annotations

import os
import signal
import subprocess
import yaml

WORKFLOW_REL = os.path.join(".github", "workflows", "riscv-ci.yml")


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
        preexec_fn=os.setsid,
    )
    try:
        stdout, stderr = proc.communicate(timeout=step_timeout_sec)
        return proc.returncode, _merge_streams(stdout, stderr)
    except subprocess.TimeoutExpired as e:
        partial = _merge_streams(e.stdout, e.stderr)
        _kill_process_tree(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc, signal.SIGKILL)
            proc.wait()
        return -1, partial


def run_workflow(workspace_dir: str, step_timeout_sec: int) -> tuple[str, str]:
    """
    读取 workspace 内 .github/workflows/riscv-ci.yml，顺序执行 steps。
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

    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            logs.append(f"\n=== Step {i + 1}: <invalid> ===\n步骤定义必须是映射。\n")
            return "failure", "".join(logs)

        name = raw.get("name", f"step-{i + 1}")
        run_cmd = raw.get("run")
        step_name = name if isinstance(name, str) else str(name)
        logs.append(f"\n=== Step: {step_name} ===\n")

        if not isinstance(run_cmd, str) or not run_cmd.strip():
            logs.append("错误: 缺少有效的 run 字段。\n")
            return "failure", "".join(logs)

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

    return "success", "".join(logs)
