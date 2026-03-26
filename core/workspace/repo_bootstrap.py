"""repo init / repo sync 失败时的错误归类、诊断输出与交互式恢复。"""

from __future__ import annotations

import os
import re
import sys
from subprocess import CompletedProcess
from typing import Literal

Action = Literal["retry_same", "reinit", "wipe_workspace", "abort"]


def merge_repo_output(r: CompletedProcess[str]) -> str:
    """合并 stdout/stderr，便于展示与归类。"""
    parts: list[str] = []
    if r.stdout and r.stdout.strip():
        parts.append(r.stdout.strip())
    if r.stderr and r.stderr.strip():
        parts.append(r.stderr.strip())
    return "\n".join(parts) if parts else "(无 stdout/stderr)"


def classify_repo_error(text: str) -> tuple[str, str]:
    """
    根据 repo/git 输出做粗粒度归类（非穷尽，用于提示「可能原因」）。
    返回 (英文类别 id, 中文说明)。
    """
    t = (text or "").lower()

    # repo 脚本首次会在工作区 .repo 下克隆「git-repo」工具本身，默认从 gerrit 拉取，国内常超时/连不上
    if (
        "gerrit.googlesource.com" in t
        or "downloading repo source" in t
        or "cloning the git-repo repository failed" in t
        or "fatal: double check your --repo-rev" in t
    ):
        return (
            "repo_tool_clone_failed",
            "repo init 时会克隆 git-repo 工具源码（默认源 gerrit.googlesource.com），"
            "若无法访问该域名会失败。请在 .env 中设置 REPO_REPO_URL 指向可访问镜像，"
            "并重新 init。示例：REPO_REPO_URL=https://mirrors.tuna.tsinghua.edu.cn/git/git-repo "
            "或 REPO_REPO_URL=https://github.com/git-repo/git-repo；也可配置 HTTPS 代理后再试。",
        )

    if (
        "requires repo to be installed" in t
        or 'use "repo init"' in t
        or ("repo init" in t and "install" in t and "command" in t)
    ):
        return (
            "not_initialized",
            "repo 认为当前目录尚未完成 init（缺少 .repo 或元数据不完整）。"
            "若刚删过 .repo 或 init 未成功结束，会出现此提示。",
        )

    if "unparseable head" in t or ("unparseable" in t and "head" in t):
        return (
            "corrupted_repo_meta",
            ".repo 内某个 Git 仓库的 HEAD 异常（常见于手动删改 .repo 或同步中断）。",
        )

    if "downloading network" in t or "network changes failed" in t:
        return (
            "network_sync",
            "repo 报告网络同步失败（多项目并行拉取时某一仓库失败）。",
        )

    if "unable to remote fetch" in t or "failing repos (network)" in t:
        return ("network_fetch", "远端拉取失败：网络不稳定、代理、DNS 或对 GitHub 访问受限。")

    if "authentication failed" in t or "could not read username" in t:
        return ("auth", "Git 鉴权失败：检查 token、SSH 与仓库访问权限。")

    if "403" in text or "401" in text:
        return ("http_auth", "HTTP 403/401：Token 权限不足、过期或仓库为私有但未授权。")

    if "could not resolve host" in t or "name or service not known" in t:
        return ("dns", "DNS 无法解析主机名，检查网络与 resolv 配置。")

    if "connection timed out" in t or "timed out" in t:
        return ("timeout", "连接超时：网络质量差、防火墙或需要代理。")

    if "syncerror" in t or "unable to fully sync" in t:
        return ("sync_partial", "同步未完全成功：可能含网络或单个 project 失败，可重试或查看上方具体 project 名。")

    return ("unknown", "未匹配到已知模式，请根据完整输出排查（网络、权限、manifest、磁盘空间等）。")


def interactive_bootstrap_enabled() -> bool:
    """
    是否启用交互菜单。
    无 TTY、或设置 REPO_BOOTSTRAP_NON_INTERACTIVE=1、或 CI=true 时关闭交互，仅打印说明后退出。
    """
    v = os.environ.get("REPO_BOOTSTRAP_NON_INTERACTIVE", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return False
    ci = os.environ.get("CI", "").strip().lower()
    if ci in ("1", "true", "yes"):
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError):
        return False


def print_diagnostic(
    *,
    stage: str,
    workspace_dir: str,
    r: CompletedProcess[str],
    category: str,
    explanation_zh: str,
) -> None:
    err = merge_repo_output(r)
    print("\n" + "=" * 72, flush=True)
    print(f"[错误] repo {stage} 失败", flush=True)
    print("=" * 72, flush=True)
    print(f"工作区目录: {workspace_dir}", flush=True)
    print(f"进程退出码: {r.returncode}", flush=True)
    print(f"错误归类: {category}", flush=True)
    print(f"可能原因: {explanation_zh}", flush=True)
    print("\n--- 完整命令输出（stdout + stderr 合并）---\n", flush=True)
    print(err, flush=True)
    print("\n--- 输出结束 ---", flush=True)


def print_non_interactive_hint(stage: str) -> None:
    print(
        "\n当前为非交互模式（无终端 TTY，或已设置 REPO_BOOTSTRAP_NON_INTERACTIVE=1 / CI=true）。\n"
        "请在本机终端前台运行 Runner，或手动执行以下一类操作后重试：\n"
        "  • 仅重试：再次运行；或在工作区内执行 `repo sync -j1 --fail-fast --no-clone-bundle`。\n"
        "  • 重建 .repo：`rm -rf <WORKSPACE>/.repo` 后重新启动 Runner。\n"
        "  • 清空工作区：备份必要文件后 `rm -rf <WORKSPACE>/*` 再启动。\n",
        flush=True,
    )


def prompt_choice(stage: str) -> Action:
    """循环读取用户输入，直到得到合法选项。"""
    if stage == "init":
        print("\n请选择处理方式：", flush=True)
        print("  1) 仅重试 repo init（适合网络抖动或临时失败）", flush=True)
        print("  2) 删除 .repo 后重新 repo init（适合元数据损坏或不完整）", flush=True)
        print(
            "  3) 清空工作区目录内全部内容后重来（严重损坏时；请先备份 workflow 等到别处）",
            flush=True,
        )
        print("  4) 退出 Runner", flush=True)
    else:
        print("\n请选择处理方式：", flush=True)
        print("  1) 仅重试 repo sync（不删 .repo，适合网络抖动）", flush=True)
        print(
            "  2) 删除 .repo 后重新 repo init + repo sync（适合 manifest 元数据损坏）",
            flush=True,
        )
        print(
            "  3) 清空工作区目录内全部内容后重来（请先备份需要保留的文件）",
            flush=True,
        )
        print("  4) 退出 Runner", flush=True)

    while True:
        try:
            raw = input("请输入选项编号 [1-4]: ").strip()
        except EOFError:
            print("[Info] 收到 EOF，按退出处理。", flush=True)
            return "abort"
        if not raw:
            print("输入为空，请输入 1～4。", flush=True)
            continue
        if not re.fullmatch(r"[1-4]", raw):
            print("无效输入：请只输入单个数字 1、2、3 或 4。", flush=True)
            continue
        return {
            "1": "retry_same",
            "2": "reinit",
            "3": "wipe_workspace",
            "4": "abort",
        }[raw]


def raise_noninteractive_failure(
    *,
    stage: str,
    workspace_dir: str,
    r: CompletedProcess[str],
    category: str,
    explanation_zh: str,
) -> None:
    print_diagnostic(
        stage=stage,
        workspace_dir=workspace_dir,
        r=r,
        category=category,
        explanation_zh=explanation_zh,
    )
    print_non_interactive_hint(stage)
    err = merge_repo_output(r)
    raise RuntimeError(
        f"repo {stage} 失败（非交互模式已中止）。归类={category}，退出码={r.returncode}。\n{err}"
    )
