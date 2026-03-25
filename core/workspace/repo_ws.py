"""多仓库 repo 工作区实现（使用 Google repo 工具管理 manifest）。"""

from __future__ import annotations

import os
import subprocess
import threading
import xml.etree.ElementTree as ET
from urllib.parse import quote

from core.workspace import PRInfo, WorkspaceBase


def _authed_url(repo: str, token: str) -> str:
    safe = quote(token, safe="")
    return f"https://x-access-token:{safe}@github.com/{repo}.git"


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

    def bootstrap(self) -> None:
        os.makedirs(self._workspace_dir, exist_ok=True)
        repo_meta = os.path.join(self._workspace_dir, ".repo")

        if not os.path.isdir(repo_meta):
            manifest_url = _authed_url(self._manifest_repo, self._token)
            r = subprocess.run(
                [
                    "repo", "init",
                    "-u", manifest_url,
                    "-b", self._manifest_branch,
                    "-m", self._manifest_file,
                ],
                cwd=self._workspace_dir,
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip()
                raise RuntimeError(f"repo init 失败 (exit {r.returncode}): {err}")

        r = subprocess.run(
            ["repo", "sync", "-j4", "--no-clone-bundle", "--force-sync"],
            cwd=self._workspace_dir,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            raise RuntimeError(f"repo sync 全量失败 (exit {r.returncode}): {err}")

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
        # 1. 增量同步所有子仓库（-c 只同步当前分支，速度快）
        r = subprocess.run(
            ["repo", "sync", "-j4", "--no-clone-bundle", "-c"],
            cwd=self._workspace_dir,
            capture_output=True,
            text=True,
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
