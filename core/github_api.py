"""GitHub Commits API / Pull Requests API 与 Checks API（Check Runs）。"""

from __future__ import annotations

import os

import requests

API_BASE = "https://api.github.com"
CHECK_NAME = "RISC-V Native CI"
LOG_TRUNCATE_MARKER = "\n...[Log Truncated by RiscvLiteRunner]...\n"
LOG_MAX_CHARS = 60000
GITHUB_OUTPUT_TEXT_MAX = 65535


def _github_headers() -> dict[str, str]:
    token = os.environ["GITHUB_TOKEN"]
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def get_latest_sha() -> str:
    repo = os.environ["GITHUB_REPO"]
    branch = os.environ["TARGET_BRANCH"]
    url = f"{API_BASE}/repos/{repo}/commits/{branch}"
    r = requests.get(url, headers=_github_headers(), timeout=60)
    r.raise_for_status()
    data = r.json()
    sha = data.get("sha")
    if not sha or not isinstance(sha, str):
        raise RuntimeError("API 响应中缺少有效的 sha 字段")
    return sha


def list_open_prs() -> list[dict[str, object]]:
    """
    返回 open PR 列表，按创建时间升序（oldest first）。
    每个元素包含 number(int), head_sha(str), title(str), draft(bool),
    labels(list[str]), created_at(str), updated_at(str)。
    """
    repo = os.environ["GITHUB_REPO"]
    branch = os.environ["TARGET_BRANCH"]
    url = f"{API_BASE}/repos/{repo}/pulls"
    params = {
        "state": "open",
        "base": branch,
        "sort": "created",
        "direction": "asc",
        "per_page": 100,
    }
    r = requests.get(url, headers=_github_headers(), params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("PR 列表 API 返回格式异常")

    prs: list[dict[str, object]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        title = item.get("title", "")
        draft = bool(item.get("draft", False))
        created_at = item.get("created_at", "")
        updated_at = item.get("updated_at", "")
        raw_labels = item.get("labels", [])
        labels: list[str] = []
        if isinstance(raw_labels, list):
            for lab in raw_labels:
                if isinstance(lab, dict):
                    name = lab.get("name")
                    if isinstance(name, str) and name:
                        labels.append(name)
        head = item.get("head", {})
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if isinstance(number, int) and isinstance(head_sha, str):
            prs.append(
                {
                    "number": number,
                    "head_sha": head_sha,
                    "title": title,
                    "draft": draft,
                    "labels": labels,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
    return prs


def create_check_run(sha: str) -> int:
    repo = os.environ["GITHUB_REPO"]
    url = f"{API_BASE}/repos/{repo}/check-runs"
    body = {
        "name": CHECK_NAME,
        "head_sha": sha,
        "status": "in_progress",
    }
    r = requests.post(url, headers=_github_headers(), json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    cid = data.get("id")
    if cid is None:
        raise RuntimeError("check-runs 响应缺少 id 字段")
    return int(cid)


def _truncate_log_for_github(log_output: str) -> str:
    if len(log_output) <= LOG_MAX_CHARS:
        return log_output
    return LOG_TRUNCATE_MARKER + log_output[-LOG_MAX_CHARS:]


def update_check_run(check_run_id: int, conclusion: str, log_output: str) -> None:
    if conclusion not in ("success", "failure"):
        raise ValueError('conclusion 只能是 "success" 或 "failure"')
    inner = _truncate_log_for_github(log_output)
    text = f"```{inner}```"
    if len(text) > GITHUB_OUTPUT_TEXT_MAX:
        over = len(text) - GITHUB_OUTPUT_TEXT_MAX
        inner = inner[over:]
        text = f"```{inner}```"
    repo = os.environ["GITHUB_REPO"]
    url = f"{API_BASE}/repos/{repo}/check-runs/{check_run_id}"
    body = {
        "status": "completed",
        "conclusion": conclusion,
        "output": {
            "title": "Build Logs",
            "summary": "Execution finished.",
            "text": text,
        },
    }
    r = requests.patch(url, headers=_github_headers(), json=body, timeout=120)
    r.raise_for_status()
