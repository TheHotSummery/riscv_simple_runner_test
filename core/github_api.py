"""GitHub REST API 封装：PR 列表、Commit Status、PR 评论。"""

from __future__ import annotations

import os

import requests

API_BASE = "https://api.github.com"
STATUS_CONTEXT = "RISC-V Native CI"
STATUS_DESC_MAX = 140

# ETag 缓存（per-repo），减少 API 计数消耗
_etag_cache: dict[str, str] = {}


def _github_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    token = os.environ["GITHUB_TOKEN"]
    h: dict[str, str] = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    if extra:
        h.update(extra)
    return h


def _clip(text: str, max_len: int = STATUS_DESC_MAX) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


# ── PR 列表 ───────────────────────────────────────────────────────────────────

def list_open_prs(
    repo: str,
    target_branch: str,
) -> list[dict[str, object]] | None:
    """
    返回目标分支的 open PR 列表（按创建时间升序）。
    若服务器返回 304 Not Modified，则返回 None（表示无变化）。
    使用 ETag 缓存，减少实际 API 消耗。

    每个元素包含：
      number, head_sha, title, draft, labels(list[str]),
      author(str), commit_message(str), created_at, updated_at
    """
    url = f"{API_BASE}/repos/{repo}/pulls"
    params: dict[str, object] = {
        "state": "open",
        "base": target_branch,
        "sort": "created",
        "direction": "asc",
        "per_page": 100,
    }
    extra: dict[str, str] = {}
    cached_etag = _etag_cache.get(repo)
    if cached_etag:
        extra["If-None-Match"] = cached_etag

    r = requests.get(url, headers=_github_headers(extra), params=params, timeout=60)

    if r.status_code == 304:
        return None  # 无变化

    r.raise_for_status()

    new_etag = r.headers.get("ETag")
    if new_etag:
        _etag_cache[repo] = new_etag

    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"list_open_prs: API 返回格式异常，repo={repo}")

    prs: list[dict[str, object]] = []
    for item in data:
        if not isinstance(item, dict):
            continue

        number = item.get("number")
        head = item.get("head", {})
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(number, int) or not isinstance(head_sha, str):
            continue

        # 作者 login
        author = ""
        user = item.get("user")
        if isinstance(user, dict):
            author = str(user.get("login", ""))

        # 标签名称列表
        raw_labels = item.get("labels", [])
        labels: list[str] = []
        if isinstance(raw_labels, list):
            for lab in raw_labels:
                if isinstance(lab, dict):
                    name = lab.get("name")
                    if isinstance(name, str) and name:
                        labels.append(name)

        # head commit message（用于 skip-ci 检测）
        commit_message = ""
        if isinstance(head, dict):
            commit_obj = head.get("commit", {})
            if isinstance(commit_obj, dict):
                msg = commit_obj.get("message", "")
                if isinstance(msg, str):
                    commit_message = msg

        prs.append({
            "number": number,
            "head_sha": head_sha,
            "title": item.get("title", ""),
            "draft": bool(item.get("draft", False)),
            "labels": labels,
            "author": author,
            "commit_message": commit_message,
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
        })

    return prs


# ── Commit Status ─────────────────────────────────────────────────────────────

def create_commit_status(repo: str, sha: str) -> None:
    """在 commit 上创建 pending 状态（Build queued）。"""
    url = f"{API_BASE}/repos/{repo}/statuses/{sha}"
    body = {
        "state": "pending",
        "context": STATUS_CONTEXT,
        "description": _clip("Build queued."),
    }
    r = requests.post(url, headers=_github_headers(), json=body, timeout=60)
    r.raise_for_status()


def update_commit_status_pending(repo: str, sha: str, description: str) -> None:
    """将 commit 状态更新为 pending，附带进度描述。"""
    url = f"{API_BASE}/repos/{repo}/statuses/{sha}"
    body = {
        "state": "pending",
        "context": STATUS_CONTEXT,
        "description": _clip(description),
    }
    r = requests.post(url, headers=_github_headers(), json=body, timeout=60)
    r.raise_for_status()


def update_commit_status(
    repo: str,
    sha: str,
    conclusion: str,
    description: str | None = None,
) -> None:
    """
    将 commit 状态更新为最终结果。
    conclusion: "success" | "failure"
    """
    if conclusion not in ("success", "failure"):
        raise ValueError('conclusion 只能是 "success" 或 "failure"')
    state = "success" if conclusion == "success" else "failure"
    desc = description or ("Build succeeded." if state == "success" else "Build failed.")
    url = f"{API_BASE}/repos/{repo}/statuses/{sha}"
    body = {
        "state": state,
        "context": STATUS_CONTEXT,
        "description": _clip(desc),
    }
    r = requests.post(url, headers=_github_headers(), json=body, timeout=60)
    r.raise_for_status()


# ── PR 评论 ───────────────────────────────────────────────────────────────────

def create_pr_comment(repo: str, pr_number: int, body: str) -> int:
    """在 PR 下新建评论，返回 comment_id（用于后续 edit）。"""
    url = f"{API_BASE}/repos/{repo}/issues/{pr_number}/comments"
    r = requests.post(url, headers=_github_headers(), json={"body": body}, timeout=60)
    r.raise_for_status()
    return int(r.json()["id"])


def edit_pr_comment(repo: str, comment_id: int, body: str) -> None:
    """编辑已有 PR 评论。"""
    url = f"{API_BASE}/repos/{repo}/issues/comments/{comment_id}"
    r = requests.patch(url, headers=_github_headers(), json={"body": body}, timeout=60)
    r.raise_for_status()
