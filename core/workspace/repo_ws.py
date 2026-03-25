"""多仓库 repo 工作区实现（使用 Google repo 工具管理 manifest）。"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import xml.etree.ElementTree as ET
from urllib.parse import quote

from core.config import normalize_github_repo_slug
from core.workspace import PRInfo, WorkspaceBase
from core.workspace.workflow_fallback import try_materialize_workflow_from_target_branch


def _authed_url(repo: str, token: str) -> str:
    repo = normalize_github_repo_slug(repo)
    safe = quote(token, safe="")
    return f"https://x-access-token:{safe}@github.com/{repo}.git"


def _repo_binary() -> str:
    """
    返回 PATH 中的 repo 可执行文件路径。
    未安装时抛出带安装提示的 RuntimeError（避免 obscure 的 FileNotFoundError）。
    """
    path = shutil.which("repo")
    if path:
        return path
    raise RuntimeError(
        "未找到 Google repo 工具（命令「repo」不在 PATH 中）。\n"
        "WORKSPACE_MODE=repo 必须安装该工具后再启动 Runner。\n\n"
        "安装示例：\n"
        "  • Debian/Ubuntu：sudo apt install repo\n"
        "  • 或官方脚本：\n"
        "      mkdir -p ~/.bin\n"
        "      curl -fsSL https://storage.googleapis.com/git-repo-downloads/repo -o ~/.bin/repo\n"
        "      chmod +x ~/.bin/repo\n"
        "      echo 'export PATH=\"$HOME/.bin:$PATH\"' >> ~/.bashrc\n"
        "    然后重新登录或 source ~/.bashrc，执行 which repo 确认。\n"
    )


def _run_repo(args: list[str], *, cwd: str) -> subprocess.CompletedProcess[str]:
    """以绝对路径调用 repo，避免 PATH 在 systemd 下与交互 shell 不一致。"""
    bin_path = _repo_binary()
    return subprocess.run(
        [bin_path, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


class RepoWorkspace(WorkspaceBase):
    """
    使用 repo 工具管理的多仓库工作区。

    bootstrap   : repo init（首次）+ repo sync（全量）；进程重启后只做增量 sync。
    sync_for_pr : repo sync（增量）+ 目标子仓库 git reset --hard head_sha。

    注意：
      - .riscv/workflow.yml 应放于工作区根目录（不属于任何子仓库的 .git 管辖范围内）。
        可由 manifest 里的某个子仓库 checkout 后 symlink，也可单独维护。
      - MAX_PARALLEL_JOBS > 1 时，多个 Worker 会串行等待 sync 锁，
        build 阶段仍在同一 workspace 内执行，暂不支持真正并行（需多工作区）。
    """

    def __init__(
        self,
        workspace_dir: str,
        manifest_repo: str,
        manifest_branch: str,
        manifest_file: str,
        token: str,
        manifest_github_org: str = "",
    ) -> None:
        self._workspace_dir = workspace_dir
        self._manifest_repo = manifest_repo
        self._manifest_branch = manifest_branch
        self._manifest_file = manifest_file
        self._token = token
        self._manifest_github_org = manifest_github_org
        # {github_repo_name_or_path -> local_relative_path}
        self._repo_path_map: dict[str, str] = {}
        # 串行化 sync 操作，防止多 Worker 并发写同一工作区
        self._sync_lock = threading.Lock()

    # ── WorkspaceBase 实现 ────────────────────────────────────────────────

    def _repo_init(self) -> None:
        """若尚无 .repo，则 repo init。"""
        repo_meta = os.path.join(self._workspace_dir, ".repo")
        if os.path.isdir(repo_meta):
            return
        manifest_url = _authed_url(self._manifest_repo, self._token)
        r = _run_repo(
            [
                "init",
                "-u", manifest_url,
                "-b", self._manifest_branch,
                "-m", self._manifest_file,
            ],
            cwd=self._workspace_dir,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(f"repo init 失败 (exit {r.returncode}): {err}")

    def _repo_sync_full(self) -> subprocess.CompletedProcess[str]:
        return _run_repo(
            ["sync", "-j4", "--no-clone-bundle", "--force-sync"],
            cwd=self._workspace_dir,
        )

    def bootstrap(self) -> None:
        _repo_binary()  # 尽早给出清晰错误，而非 FileNotFoundError
        os.makedirs(self._workspace_dir, exist_ok=True)
        repo_meta = os.path.join(self._workspace_dir, ".repo")

        self._repo_init()

        r = self._repo_sync_full()
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            print(
                "[Warn] repo sync 全量失败，常见原因是 .repo 或 .repo/manifests 曾被手动删除/损坏。"
                " 将删除 .repo 后重新 init + sync（仅自动重试一次）…",
                flush=True,
            )
            print(f"[Warn] 上次错误输出：\n{err}", flush=True)
            shutil.rmtree(repo_meta, ignore_errors=True)
            self._repo_path_map = {}
            self._repo_init()
            r = self._repo_sync_full()
            if r.returncode != 0:
                err2 = (r.stderr or r.stdout or "").strip()
                raise RuntimeError(
                    f"repo sync 全量仍失败 (exit {r.returncode}): {err2}\n\n"
                    "请清空整个工作区目录后重试（会删除未纳入子仓库的本地文件，请先备份 .riscv/workflow.yml 等）：\n"
                    f"  rm -rf {self._workspace_dir!r}\n"
                    "然后重新启动 Runner。"
                )

        self._repo_path_map = self._parse_manifest()
        print(
            f"[Info] manifest 解析完成，共 {len(self._repo_path_map)} 个子仓库",
            flush=True,
        )

    def sync_for_pr(self, pr: PRInfo) -> None:
        with self._sync_lock:
            self._sync_for_pr_locked(pr)

    def workflow_dir(self) -> str:
        return self._workspace_dir

    def workflow_dir_for_pr(self, pr: PRInfo) -> str:
        """
        多仓：优先使用「当前 PR 所在子仓库」内的 .riscv/workflow.yml
        （与 GitHub 上该仓库各分支中的路径一致，例如 build/.riscv/workflow.yml）；
        若子仓没有，再退回工作区根目录下的同路径（共用一份 workflow）。
        """
        root = self.workflow_dir()
        wf_rel = os.path.join(".riscv", "workflow.yml")
        sub = self._find_sub_path(pr.repo)
        if sub:
            sub_root = os.path.join(root, sub)
            if os.path.isfile(os.path.join(sub_root, wf_rel)):
                return sub_root
        if os.path.isfile(os.path.join(root, wf_rel)):
            return root
        # 未找到文件时，让 executor 报错路径指向最可能的位置（通常应先补子仓内 workflow）
        return os.path.join(root, sub) if sub else root

    def ensure_workflow_for_build(self, pr: PRInfo, target_branch: str) -> str:
        """
        若 PR 分支（当前 HEAD）无 .riscv/workflow.yml，则从 origin/<target_branch>
        仅检出该文件到子仓库，其余文件仍为 PR 的 head_sha。
        """
        wf_dir = self.workflow_dir_for_pr(pr)
        wf_path = os.path.join(wf_dir, ".riscv", "workflow.yml")
        if os.path.isfile(wf_path):
            return wf_dir
        sub = self._find_sub_path(pr.repo)
        if sub:
            abs_sub = os.path.join(self._workspace_dir, sub)
            if try_materialize_workflow_from_target_branch(abs_sub, target_branch):
                if os.path.isfile(wf_path):
                    print(
                        f"[Info] PR 分支缺少 .riscv/workflow.yml，已从 origin/{target_branch} 检出",
                        flush=True,
                    )
                    return wf_dir
        root = self.workflow_dir()
        root_wf = os.path.join(root, ".riscv", "workflow.yml")
        if os.path.isfile(root_wf):
            print(
                "[Info] 使用工作区根目录下的 .riscv/workflow.yml（子仓内仍无该文件）",
                flush=True,
            )
            return root
        return wf_dir

    # ── 公共工具方法 ──────────────────────────────────────────────────────

    def discover_watch_repos(self) -> list[str]:
        """返回 manifest 里所有子仓库的「完整名称」列表，供 Poller 使用。"""
        if not self._repo_path_map:
            self._repo_path_map = self._parse_manifest()
        return list(self._repo_path_map.keys())

    def sub_repo_local_path(self, repo: str) -> str | None:
        """返回子仓库在工作区内的相对路径，找不到返回 None。"""
        return self._find_sub_path(repo)

    # ── 私有方法 ──────────────────────────────────────────────────────────

    def _sync_for_pr_locked(self, pr: PRInfo) -> None:
        _repo_binary()
        # 1. 增量同步所有子仓库（-c 只同步当前分支，速度快）
        r = _run_repo(
            ["sync", "-j4", "--no-clone-bundle", "-c"],
            cwd=self._workspace_dir,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(f"repo sync 增量失败 (exit {r.returncode}): {err}")

        # 进程重启后 map 为空，重新解析
        if not self._repo_path_map:
            self._repo_path_map = self._parse_manifest()

        # 2. 将目标子仓库切换到 PR head SHA
        sub_path = self._find_sub_path(pr.repo)
        if sub_path is None:
            raise RuntimeError(
                f"在 manifest 中找不到仓库 {pr.repo!r}。"
                f"已知仓库：{list(self._repo_path_map.keys())}"
            )

        abs_sub = os.path.join(self._workspace_dir, sub_path)

        fetch = subprocess.run(
            ["git", "-C", abs_sub, "fetch", "origin"],
            capture_output=True,
            text=True,
        )
        if fetch.returncode != 0:
            err = (fetch.stderr or fetch.stdout or "").strip()
            raise RuntimeError(
                f"git fetch 子仓库 {pr.repo!r} 失败 (exit {fetch.returncode}): {err}"
            )

        reset = subprocess.run(
            ["git", "-C", abs_sub, "reset", "--hard", pr.head_sha],
            capture_output=True,
            text=True,
        )
        if reset.returncode != 0:
            err = (reset.stderr or reset.stdout or "").strip()
            raise RuntimeError(
                f"git reset --hard 子仓库 {pr.repo!r} 失败 (exit {reset.returncode}): {err}"
            )

    def _find_sub_path(self, repo: str) -> str | None:
        """
        查找顺序（优先级递减）：
        1. 精确匹配 "org/name"
        2. 只匹配 name 部分（不带 org）
        3. 任意以 name 结尾的 key
        """
        if repo in self._repo_path_map:
            return self._repo_path_map[repo]

        name = repo.split("/")[-1]
        if name in self._repo_path_map:
            return self._repo_path_map[name]

        for key, path in self._repo_path_map.items():
            if key.split("/")[-1] == name:
                return path

        return None

    @staticmethod
    def _infer_github_org_from_fetch(fetch: str) -> str | None:
        """
        当 remote fetch 不是完整 https URL 时，尽量推断 GitHub owner。

        常见 manifest 写法：
          fetch="../spacemit-robotics"   → owner = spacemit-robotics
          fetch="spacemit-robotics"      → owner = spacemit-robotics
        与 list_open_prs(repo) 所需的 owner/repo 对齐。
        """
        fetch = fetch.strip().rstrip("/")
        if not fetch or "github.com" in fetch:
            return None
        parts = [p for p in fetch.replace("\\", "/").split("/") if p and p not in (".",)]
        if not parts:
            return None
        # 取最后一段作为组织/用户目录名（与 repo 工具解析相对 remote 的习惯一致）
        return parts[-1]

    def _parse_manifest(self) -> dict[str, str]:
        """
        解析 .repo/manifests/{manifest_file}（或 .repo/manifest.xml），
        返回 {repo_full_name: local_relative_path}。
        repo_full_name 尽量保留 "org/name" 格式（若 remote fetch 含 github.com）。
        """
        candidates = [
            os.path.join(self._workspace_dir, ".repo", "manifests", self._manifest_file),
            os.path.join(self._workspace_dir, ".repo", "manifest.xml"),
        ]
        manifest_path = next((p for p in candidates if os.path.isfile(p)), None)
        if manifest_path is None:
            print(
                f"[Warn] 找不到 manifest 文件（已尝试：{candidates}）",
                flush=True,
            )
            return {}

        try:
            tree = ET.parse(manifest_path)
            root = tree.getroot()
        except ET.ParseError as e:
            print(f"[Warn] manifest XML 解析失败: {e}", flush=True)
            return {}

        # 收集 remote 定义
        remotes: dict[str, str] = {}   # remote_name -> fetch url prefix
        for remote_el in root.findall("remote"):
            r_name = remote_el.get("name", "")
            r_fetch = remote_el.get("fetch", "")
            if r_name:
                remotes[r_name] = r_fetch

        default_remote = ""
        default_el = root.find("default")
        if default_el is not None:
            default_remote = default_el.get("remote", "")

        result: dict[str, str] = {}
        for project in root.findall("project"):
            name = project.get("name", "")
            if not name:
                continue
            local_path = project.get("path", name)
            remote_name = project.get("remote", default_remote)
            fetch_prefix = remotes.get(remote_name, "")

            # 尽量重建 "org/name" 格式（与 GitHub API 的 repo 全名一致）
            if "github.com" in fetch_prefix:
                # fetch 形如 "https://github.com/org/" 或 "git@github.com:org"
                after_gh = fetch_prefix.split("github.com")[-1].lstrip(":/").rstrip("/")
                full_name = f"{after_gh}/{name}" if after_gh and "/" not in name else name
            else:
                org = (
                    self._manifest_github_org.strip()
                    or self._infer_github_org_from_fetch(fetch_prefix)
                )
                if org and "/" not in name:
                    full_name = f"{org}/{name}"
                else:
                    full_name = name

            result[full_name] = local_path

        return result
