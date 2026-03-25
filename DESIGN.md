# RISC-V Action Runner — 设计文档

> 状态：**设计评审中**，尚未开始编码。  
> 本文档记录当前已发现的问题、完整的重构设计思路、各模块实现方案，以及待确认事项。

---

## 目录

1. [现有问题清单](#1-现有问题清单)
2. [新架构总览](#2-新架构总览)
3. [模块设计](#3-模块设计)
   - 3.1 Config
   - 3.2 工作区抽象层（Workspace）
   - 3.3 PR 发现层（Poller）
   - 3.4 任务队列（JobQueue）
   - 3.5 执行层（Executor）
   - 3.6 上报层（Reporter）
   - 3.7 主协调器（main）
4. [数据流图](#4-数据流图)
5. [文件目录结构](#5-文件目录结构)
6. [配置项全表](#6-配置项全表)
7. [迁移路径](#7-迁移路径)
8. [安全与执行注意事项](#8-安全与执行注意事项)
9. [待确认事项](#9-待确认事项)
10. [Phase 0 实施清单](#10-phase-0-实施清单)

---

## 1 现有问题清单

### P0：立即修复（影响正确性）

#### 1.1 `.github/workflows/` 命名冲突（红叉来源）

- **现象**：GitHub 自动扫描 `.github/workflows/*.yml`，将 `riscv-ci.yml` 当作 GitHub Actions 原生工作流执行，因格式不符或找不到 runner，在 Actions 标签页报红叉。
- **影响**：PR 页面同时出现两套 CI 状态（自建 runner 的 Commit Status + GitHub Actions 的失败），用户困惑。
- **修复**：将自定义工作流文件迁移到 `.riscv/workflow.yml`（或其他非 `.github/workflows/` 路径），并同步更新 `executor.py` 中 `WORKFLOW_REL` 常量。

#### 1.2 `git_manager` 不兼容 `repo` 多仓工作区

- **现象**：`sync_to_sha` 依赖顶层 `.git` 目录，而 `repo` 管理的工作区根目录只有 `.repo/`，不存在顶层 `.git`。
- **影响**：如果 `WORKSPACE_MODE=repo`，工作区同步逻辑完全无效。
- **修复**：抽象 `WorkspaceBase` 接口，`GitWorkspace` 保留原逻辑，新增 `RepoWorkspace`（见 3.2 节）。

---

### P1：高优先级（影响体验与正确性）

#### 1.3 PR 作者无法感知队列状态

- **现象**：构建触发后，PR 页面只有一个小圆点状态（pending），没有：队列位置、正在执行哪个步骤、预计剩余时间、失败原因摘要。
- **修复**：入队 / 开始 / 完成三个时机分别**创建或编辑 PR 评论**（复用同一条评论 ID，避免刷屏）。

#### 1.4 Runner 离线时状态永远 pending

- **现象**：进程崩溃或网络中断后，PR 上的 Commit Status 永远停在 `pending`，无人知晓。
- **修复**：添加 watchdog 机制：超过 `N × poll_interval` 无心跳时，将所有孤立 pending 状态置为 `failure`，并在对应 PR 评论注明「Runner 离线」。

#### 1.5 多仓库 PR 无法发现

- **现象**：当前只监听单个 `GITHUB_REPO`，`repo` 工程里的几十个子仓库的 PR 完全不会被看到。
- **修复**：`WORKSPACE_MODE=repo` 时，从 manifest XML 自动解析子仓库列表（或显式配置 `WATCH_REPOS`），对每个仓库独立轮询。

---

### P2：中优先级（影响可靠性）

#### 1.6 串行队列在多 PR 场景下效率低

- **现象**：所有 PR 排在同一个串行队列里，即使它们修改的是完全不同的子仓库。
- **修复**：实现**按子仓库分槽**：同一子仓库的 job 串行，不同子仓库可并行（受 `MAX_PARALLEL_JOBS` 上限控制）。

#### 1.7 同一 PR 有新 push 时旧构建不取消

- **现象**：PR 快速连 push 5 次，每个 SHA 都排队跑一遍，前 4 次结果无意义，浪费机器时间。
- **修复**：入队时检查该 `(repo, pr_number)` 是否已有 `pending` 任务，若有则标记为 `cancelled`，只保留最新 SHA。

#### 1.8 工作区污染

- **现象**：同一工作区复用，前一次构建留下的编译产物会影响下一次，导致结果不完全可复现。
- **修复**：在 `sync_for_pr` 后增加 `clean_artifacts()` 步骤；推荐策略：`repo sync` 增量更新源码，`make clean` / `ninja -t clean` 清理编译产物，保留 `ccache` 等缓存。

#### 1.9 GitHub API 限流风险

- **现象**：多仓库 × 短轮询间隔，频繁调 `list_open_prs`，可能触碰 5000 次/小时上限。
- **修复**：`list_open_prs` 支持 `ETag` / `If-None-Match`，服务器返回 `304 Not Modified` 时不计入限流计数。

---

### P3：低优先级（增强功能）

#### 1.10 无手动重跑能力

- 目前只能通过 push 新 commit 触发，无法对失败构建一键重试。
- **修复**：监听 PR 评论，当评论内容为 `/rerun` 时重新入队当前 `head_sha`。

#### 1.11 无 `skip-ci` 支持

- 目前所有非 draft PR 都会触发，无法跳过。
- **修复**：检查 commit message 是否含 `[skip ci]` / `[ci skip]`，或 PR 是否有 `skip-ci` 标签。

#### 1.12 构建日志无法在 GitHub 上直接查看

- 日志只存在本机文件里，PR 作者必须找管理员要日志。
- **修复**：日志写入 `logs/{job_id}.log`，PR 评论里附日志末尾 N 行摘要 + 本机可访问的文件路径（或上传到内网文件服务）。

#### 1.13 Commit Status 可升级为 Checks API

- Commit Status 只有小圆点；Checks API 支持步骤详情、内嵌日志、Annotations、Re-run 按钮、Summary Markdown。
- **前提**：需要 GitHub App 安装 token（Fine-grained PAT 也可用于 `checks:write`）。
- **现状**：暂不强制要求，作为未来升级路径记录。

---

## 2 新架构总览

```
┌────────────────────────────────────────────────────────────────┐
│  主线程                                                         │
│  Poller.poll_once()  ──每 poll_interval 秒──►  JobQueue.enqueue│
│    ├─ 对每个 watch_repo 调用 list_open_prs（带 ETag）           │
│    ├─ 比较 pr_state，跳过未变化 / draft                        │
│    └─ cancel_superseded → 入队新 BuildJob                      │
└───────────────────────────────┬────────────────────────────────┘
                                │
                    ┌───────────▼──────────┐
                    │   JobQueue           │
                    │  per-repo FIFO 槽    │
                    │  cancel_for_pr()     │
                    │  get_next_runnable() │
                    └───────────┬──────────┘
                                │  Worker 线程（最多 max_parallel_jobs 个）
               ┌────────────────▼────────────────────────────────┐
               │  Worker Loop                                     │
               │  1. workspace.sync_for_pr(pr)                   │
               │       ├─ GitWorkspace: fetch + reset --hard      │
               │       └─ RepoWorkspace: repo sync + sub checkout │
               │  2. workspace.clean_artifacts()                  │
               │  3. executor.run_workflow()                      │
               │       逐步执行 .riscv/workflow.yml steps         │
               │       progress_cb → reporter.step_progress()    │
               │  4. reporter.job_done()                         │
               │       ├─ update_commit_status(success/failure)  │
               │       ├─ write_log(job_id, log_text)            │
               │       └─ edit_pr_comment(result + log tail)     │
               └─────────────────────────────────────────────────┘
                                │
                    ┌───────────▼──────────┐
                    │  持久化              │
                    │  .pr_state.json      │
                    │  logs/{job_id}.log   │
                    └──────────────────────┘
```

**Watchdog 线程**（独立）：定期检查心跳文件，超时则清理 pending 状态 + 发 PR 警告评论。

---

## 3 模块设计

### 3.1 Config（`core/config.py`）

在原有字段基础上新增：

```
# 工作区模式
WORKSPACE_MODE        = "git" | "repo"    默认 "git"

# repo 模式专有
MANIFEST_REPO         = "org/manifest"
MANIFEST_BRANCH       = "main"
MANIFEST_FILE         = "default.xml"
WATCH_REPOS           = "org/r1,org/r2"  留空则从 manifest 自动解析

# 调度
MAX_PARALLEL_JOBS     = 1                 同时跑几个 job（不同 repo 才可并行）
CANCEL_SUPERSEDED     = true              同 PR 新 push 时取消旧 pending

# 上报
POST_PR_COMMENT       = true             构建开始/完成时在 PR 贴/编辑评论
LOG_DIR               = "./logs"

# 安全
RUNNER_BOARD          = "deb1"           显示在状态描述里（已有）
ALLOWED_AUTHORS       = ""               留空=不限；填 GitHub login 白名单（逗号分隔）
```

**单仓库兼容**：`WORKSPACE_MODE=git` 时，`GITHUB_REPO` 为唯一监听仓库，`WATCH_REPOS` 自动填充为 `[GITHUB_REPO]`，行为与当前版本完全一致。

---

### 3.2 工作区抽象层（`core/workspace/`）

#### 接口定义（`__init__.py`）

```python
class WorkspaceBase(ABC):
    def bootstrap(self) -> None:
        """首次初始化：git clone 或 repo init + repo sync（全量，只做一次）"""

    def sync_for_pr(self, pr: PRInfo) -> None:
        """
        将工作区调整到「可构建此 PR 的状态」。
        git 模式：fetch origin + reset --hard head_sha
        repo 模式：repo sync（增量）+ 目标子仓库 git checkout head_sha
        """

    def clean_artifacts(self) -> None:
        """
        清理编译产物，保留源码 cache。
        默认实现为空（由 workflow.yml 的 clean step 负责）。
        子类可覆盖以执行 make clean 等。
        """

    def sub_repo_path(self, repo: str) -> str | None:
        """
        返回子仓库在工作区内的相对路径。
        git 模式：返回 workspace_dir 本身。
        repo 模式：从 manifest 解析 path 字段。
        """
```

#### `GitWorkspace`（`git_ws.py`）

- `bootstrap`：若无 `.git` 则 `git clone`
- `sync_for_pr`：`git fetch origin` + `git reset --hard {sha}`
- 凭据：带 token 的 HTTPS URL（原 `_authed_clone_url` 逻辑保留）

#### `RepoWorkspace`（`repo_ws.py`）

- `bootstrap`：
  1. 若无 `.repo` 则 `repo init -u {manifest_url} -b {branch} -m {file}`
  2. `repo sync -j4 --no-clone-bundle`（全量）
  3. 解析 `.repo/manifests/{manifest_file}` XML，建立 `{github_repo_name → local_path}` 映射
- `sync_for_pr`：
  1. `repo sync -j4 --no-clone-bundle -c`（增量，只同步当前分支）
  2. 找到目标子仓库本地路径
  3. `git -C {sub_path} fetch origin`
  4. `git -C {sub_path} reset --hard {sha}`
- `clean_artifacts`：默认空实现（项目可在 workflow 里自行 clean）

**manifest 解析**：读 `.repo/manifests/{manifest_file}` XML，提取每个 `<project>` 的 `name`（GitHub repo 名）和 `path`（本地目录）。

---

### 3.3 PR 发现层（`core/poller.py`）

```
Poller
  属性：
    watch_repos: list[str]        # 要监听的仓库列表
    target_branch: str
    _etags: dict[str, str]        # repo → 上次 ETag

  方法：
    poll_once(state, queue) → None
      对每个 repo：
        调用 github_api.list_open_prs(repo, etag=...)
        若返回 None（304）→ 跳过
        遍历 PR：
          跳过 draft
          跳过 ALLOWED_AUTHORS 不在白名单的（若配置了白名单）
          跳过 [skip ci] commit message（需额外 API 调用，可按需开启）
          比较 state.get((repo, pr_number)) == head_sha → 跳过
          否则：
            queue.cancel_for_pr(repo, pr_number)  # 取消旧 pending
            queue.enqueue(BuildJob(...))
            reporter.on_enqueue(repo, pr_number, sha, queue_position)
```

`github_api.list_open_prs` 扩展：
- 新增 `etag` 参数，请求头加 `If-None-Match`
- 返回值：`list[dict] | None`（None 表示 304，无变化）
- 在函数内更新并返回新 ETag（存入 `Poller._etags`）

---

### 3.4 任务队列（`core/queue.py`）

```
BuildJob（dataclass）：
  job_id: str           # uuid4
  repo: str
  pr_number: int
  head_sha: str
  status: str           # "pending" | "running" | "done" | "cancelled"
  created_at: float
  comment_id: int | None  # PR 评论 ID，供后续编辑

JobQueue：
  内部结构：
    _pending: deque[BuildJob]     # 按入队顺序
    _running: list[BuildJob]      # 正在执行（最多 max_parallel_jobs 个）
    _lock: threading.Lock

  方法：
    enqueue(job)
      加锁，追加到 _pending 末尾

    cancel_for_pr(repo, pr_number)
      加锁，将 _pending 中所有匹配的 job 标记为 cancelled
      （正在运行的 job 不中断）

    get_next_runnable(running_repos: set[str]) → BuildJob | None
      加锁，从 _pending 头部找第一个：
        status == "pending" 且 repo 不在 running_repos 中
      找到则从 _pending 移除，加入 _running，返回

    mark_done(job_id)
      加锁，从 _running 移除对应 job

    queue_position(repo, pr_number) → int
      返回该 PR 在 pending 队列中的位置（1-indexed），-1 表示不在队列
```

---

### 3.5 执行层（`core/executor.py`）

**主要变更**：

1. `WORKFLOW_REL` 常量修改为 `.riscv/workflow.yml`（解决 P0-1.1 问题）
2. 其余逻辑（进程组、超时、流式日志）基本保留
3. 新增：`run_workflow` 返回的 `log_text` 由调用方负责写入 `logs/{job_id}.log`（不在 executor 内部写，保持单一职责）

---

### 3.6 上报层（`core/reporter.py`）

替代原 `github_api.py` 中零散的状态更新调用，集中管理所有「写回 GitHub」的操作。

```
Reporter
  方法：
    on_enqueue(repo, pr_number, sha, queue_pos)
      → create_commit_status(repo, sha, "pending", "Build queued (#N in queue)")
      → create_pr_comment(repo, pr_number, body=入队模板)
        保存返回的 comment_id 到 BuildJob.comment_id

    on_start(repo, pr_number, sha, comment_id)
      → update_commit_status(repo, sha, "pending", "Build started...")
      → edit_pr_comment(repo, comment_id, body=开始模板)

    step_progress(repo, sha, comment_id, desc)
      → update_commit_status(repo, sha, "pending", desc)
      （PR 评论不在每步更新，避免 API 过于频繁）

    on_done(repo, pr_number, sha, comment_id, conclusion, log_path)
      → update_commit_status(repo, sha, conclusion, desc)
      → edit_pr_comment(repo, comment_id, body=完成模板，含日志末尾 30 行)

    on_runner_offline(pending_jobs: list[BuildJob])
      → 对每个 job：update_commit_status("failure", "Runner offline")
      → 对每个 job：edit_pr_comment("⚠️ Runner 已离线，请联系管理员重新触发")
```

**PR 评论模板（Markdown）**：

```
入队：
⏳ **RISC-V CI 已入队**
- 队列位置：#3（前方有 2 个任务）
- 机器：deb1
- 触发 SHA：`abc1234`

开始：
🔨 **RISC-V CI 构建中**
- 机器：deb1 | 步骤：1/4 `fetch-deps`
- 触发 SHA：`abc1234`

成功：
✅ **RISC-V CI 构建成功**（耗时 342s）
- 机器：deb1
<details><summary>日志末尾</summary>

\```
...最后 30 行...
\```
</details>

失败：
❌ **RISC-V CI 构建失败**（步骤：`build-kernel`，退出码 1）
- 机器：deb1
<details><summary>日志末尾</summary>

\```
...最后 30 行...
\```
</details>
```

---

### 3.7 主协调器（`core/main.py`）

**主要变化**：

- 原来的 `_tick()` 单循环 → 拆为「轮询线程」+「N 个 Worker 线程」
- `main()` 负责初始化各组件、启动线程、优雅退出

```
main() 流程：
  1. load_dotenv() + load_config()
  2. make_workspace(cfg) → GitWorkspace 或 RepoWorkspace
  3. workspace.bootstrap()
  4. state = read_pr_state()   格式变为 {(repo, pr_number): sha}FM24C04D-SO-T-G
  5. queue = JobQueue(...)
  6. reporter = Reporter(cfg)
  7. poller = Poller(cfg.watch_repos, cfg.target_branch)
  8. 启动 cfg.max_parallel_jobs 个 worker 线程
  9. 启动 1 个 watchdog 线程
  10. 主线程轮询：
      while running:
          poller.poll_once(state, queue, reporter)
          _sleep_interruptible(cfg.poll_interval)

worker_loop(queue, workspace, executor, reporter, state, lock, cfg):
  while running:
      with lock:
          running_repos = {j.repo for j in queue.running_jobs()}
          job = queue.get_next_runnable(running_repos)
      if job is None:
          sleep(1); continue
      try:
          reporter.on_start(...)
          workspace.sync_for_pr(PRInfo(...))
          workspace.clean_artifacts()
          def progress_cb(event, idx, total, step_name):
              reporter.step_progress(...)
          conclusion, log_text = executor.run_workflow(
              workspace_dir, cfg.step_timeout, progress_cb
          )
          log_path = write_log(cfg.log_dir, job.job_id, log_text)
          reporter.on_done(..., conclusion, log_path)
      except Exception as e:
          reporter.on_done(..., "failure", ...)
      finally:
          queue.mark_done(job.job_id)
          with lock:
              state[(job.repo, job.pr_number)] = job.head_sha
              write_pr_state(state)

watchdog_loop(queue, reporter, cfg):
  heartbeat_file = ".runner_heartbeat"
  while running:
      touch(heartbeat_file)                     # 写当前时间戳
      sleep(cfg.poll_interval)
  # 也可在独立进程里读 heartbeat_file，超过阈值则告警
```

---

## 4 数据流图

```
GitHub API
    │
    │ list_open_prs (ETag)
    ▼
Poller.poll_once()
    │ 比较 pr_state
    │ cancel 旧 pending
    ▼
JobQueue（per-repo FIFO）
    │
    │ get_next_runnable()
    ▼
Worker Thread
    ├─► workspace.sync_for_pr()
    │       git: fetch + reset
    │       repo: repo sync + sub checkout
    ├─► workspace.clean_artifacts()
    ├─► executor.run_workflow()
    │       读 .riscv/workflow.yml
    │       逐步执行 shell steps
    │       progress_cb → reporter.step_progress()
    └─► reporter.on_done()
            ├─► update_commit_status
            ├─► write_log → logs/{job_id}.log
            └─► edit_pr_comment（含日志摘要）
    │
    ▼
write_pr_state(.pr_state.json)
    {(repo, pr_number): sha}
```

---

## 5 文件目录结构

```
riscv-action-runner/
├── core/
│   ├── config.py           扩展配置（新增字段）
│   ├── poller.py           PR 发现（NEW）
│   ├── queue.py            任务队列（NEW）
│   ├── workspace/
│   │   ├── __init__.py     WorkspaceBase + PRInfo（NEW）
│   │   ├── git_ws.py       单仓实现（原 git_manager.py 改写）
│   │   └── repo_ws.py      多仓 repo 实现（NEW）
│   ├── executor.py         基本保留，改 WORKFLOW_REL 路径
│   ├── reporter.py         集中上报（NEW，替代分散的 github_api 调用）
│   ├── github_api.py       保留纯 API 调用，新增 ETag + PR comment 接口
│   └── main.py             重构为多线程协调
├── logs/                   构建日志（按 job_id）
│   └── .gitkeep
├── .riscv/
│   └── workflow.yml.example   工作流模板示例（NEW，替代原 .github/workflows/）
├── .pr_state.json          运行时状态（格式升级）
├── .env                    环境变量（新增配置项）
├── .env.example            示例（同步更新）FM24C04D-SO-T-G
└── riscv-runner.service
```

---

## 6 配置项全表

| 环境变量 | 必填 | 默认值 | 说明 |
|----------|------|--------|------|
| `GITHUB_TOKEN` | ✅ | — | GitHub PAT，需 `repo` + `checks:write`（或 `repo:status`）权限 |
| `GITHUB_REPO` | git 模式必填 | — | `org/repo`，单仓监听目标 |
| `TARGET_BRANCH` | ✅ | `main` | PR 目标基准分支 |
| `WORKSPACE_MODE` | | `git` | `git`（单仓）或 `repo`（多仓 manifest）|
| `WORKSPACE_DIR` | | `./workspace` | 工作区根目录 |
| `POLL_INTERVAL` | | `15` | 轮询间隔（秒）|
| `STEP_TIMEOUT` | | `3600` | 单步超时（秒）|
| `MANIFEST_REPO` | repo 模式必填 | — | manifest 仓库，如 `org/manifest` |
| `MANIFEST_BRANCH` | | `main` | manifest 分支 |
| `MANIFEST_FILE` | | `default.xml` | manifest 文件名 |
| `WATCH_REPOS` | | 空（自动解析） | 显式指定监听的子仓库，逗号分隔；留空则从 manifest 解析 |
| `MAX_PARALLEL_JOBS` | | `1` | 最大并行 job 数（不同 repo 才可并行）|
| `CANCEL_SUPERSEDED` | | `true` | 同 PR 新 push 时取消旧 pending |
| `POST_PR_COMMENT` | | `true` | 构建事件时在 PR 贴/编辑评论 |
| `LOG_DIR` | | `./logs` | 构建日志存储目录 |
| `RUNNER_BOARD` | | `deb1` | 机器标识，显示在状态描述里 |
| `ALLOWED_AUTHORS` | | 空（不限） | 允许触发构建的 GitHub login 白名单，逗号分隔 |

---

## 7 迁移路径

按阶段实施，每阶段独立可测试，不影响已跑通的功能：

| 阶段 | 内容 | 影响范围 | 预计工作量 |
|------|------|----------|-----------|
| **Phase 0** | 将 `riscv-ci.yml` 迁移到 `.riscv/workflow.yml`，修改 `executor.py` 的 `WORKFLOW_REL` | executor.py（1 行）+ 仓库文件位置 | 极小 |
| **Phase 1** | 抽象 `WorkspaceBase`，将原 `git_manager.py` 改写为 `GitWorkspace`，接口不变 | git_manager → workspace/git_ws | 小 |
| **Phase 2** | 实现 `JobQueue` + 改造 `_pick_next_pr` 为 `Poller.poll_once` | queue.py + poller.py + main.py | 中 |
| **Phase 3** | 实现 `Reporter`（含 PR 评论），替代分散的 `github_api` 状态更新调用 | reporter.py + github_api.py + main.py | 中 |
| **Phase 4** | 实现多线程 worker + watchdog | main.py | 中 |
| **Phase 5**（可选）| 实现 `RepoWorkspace` + manifest 解析 + 多仓轮询 | workspace/repo_ws.py + poller.py | 大 |
| **Phase 6**（可选）| 升级 Checks API | github_api.py + reporter.py | 中 |

---

## 8 安全与执行注意事项

部署与编写 `.riscv/workflow.yml`（及 `.riscv/workflow.yml.example`）时，请了解以下**刻意未实现**的约束；Runner 将工作流视为**受信任的构建脚本**。

### 8.1 `sudo` 与交互式密码

- 每个 `step` 在子进程中以 **运行 Runner 的 Linux 用户** 执行，**无 TTY**。
- 若步骤中含 `sudo` 且系统要求输入密码，进程通常会 **挂起、失败或在 `STEP_TIMEOUT` 后超时**，**不会出现**交互式密码提示。
- **建议**：
  - 优先在 workflow 中避免 `sudo`，或仅使用用户目录下的工具链；
  - 若必须安装系统包：为 **CI 专用账号** 配置 **免密 sudo**（`/etc/sudoers.d/` 等），并限制允许执行的命令；或改用已预装依赖的镜像/环境；
  - 不建议以 root 长期跑 Runner（权限过大）。

### 8.2 无命令白名单 / 无静态校验

- **未**对 `run` 字段做白名单、关键字过滤或 AST 分析。
- `rm`、`mkfs`、`curl | bash`、修改 `/etc` 等命令若写在工作流里，**会按原样执行**（与多数「执行仓库内脚本」的 CI 模型一致）。
- **信任边界**：能修改 `.riscv/workflow.yml` 并合并进被构建分支的人，等价于能在 Runner 机器上执行相应 shell 权限下的任意操作。

### 8.3 与不可信 PR 的关系

- 若仓库 **公开** 且接受 **fork PR**，恶意 PR 可在工作流中执行危险命令或尝试窃取 `GITHUB_TOKEN` 环境（子进程可见）。
- 缓解方向（需流程或额外开发，**本仓库默认不包含**）：
  - 使用 `ALLOWED_AUTHORS` 等限制触发对象；
  - 分支保护 + 仅对受信分支跑完整 CI；
  - 在容器/虚拟机中执行步骤、最小化挂载与网络；
  - 自研或引入对 `run` 内容的策略扫描（维护成本高）。

### 8.4 示例文件中的命令

- `.riscv/workflow.yml.example` 中的 `sudo apt-get` 等仅为**演示**；在真实 Runner 上是否可行取决于该用户的 sudo 策略，**并非** Runner 自动提供 root 能力。

---

## 9 待确认事项

在开始编码前，以下问题需要明确：

### 9.1 工作区模式 ✅ 已确认
- [x] 同时支持 **单仓（git）模式** 和 **repo 多仓模式**，两者共用同一套调度逻辑，只在 Workspace 层分叉。
- [x] `WORKSPACE_MODE=git` 为默认值，向后兼容现有配置。
- [ ] repo 模式下，manifest 仓库地址（`MANIFEST_REPO`）和初始 `WATCH_REPOS` 待部署时填写，代码层留空自动解析。

### 9.2 工作流文件路径 ✅ 已确认
- [x] 新路径：`.riscv/workflow.yml`（避免触发 GitHub Actions）。
- [x] 目标仓库里现有的 `.github/workflows/riscv-ci.yml` **需要删除或替换为空占位**，防止 GitHub Actions 报红叉。
  - 推荐：删除旧文件，在仓库根目录创建 `.riscv/workflow.yml`。
  - 若不便删除旧文件，可将其替换为标准 GitHub Actions 格式的空 job（让它立刻以 `success` 跳过），不再影响 PR 状态。

### 9.3 PR 评论 ✅ 已确认
- [x] 启用 PR 评论（`POST_PR_COMMENT=true` 为默认值）。
- [x] 使用「入队 → 开始 → 完成」三阶段复用同一条评论 ID 的方案（见 3.6 节模板）。

### 9.4 并行度
- [x] 初始 `MAX_PARALLEL_JOBS=1`，串行稳定后再按需调大。
- [ ] 多物理机器（多 Runner 实例）暂不在本次范围，后续可通过多个独立进程分别监听不同 `WATCH_REPOS` 子集实现。

### 9.5 作者白名单 ✅ 已确认
- [x] 大部分仓库公开，少量私有；代码对两者兼容（见下方「公私仓库兼容方案」）。
- [x] `ALLOWED_AUTHORS` 配置项**保留但默认为空**（空 = 不限制，允许所有 PR 作者触发构建）。
- [x] 启用白名单后，不在列表内的 PR 会被 **静默跳过**（不报错、不更新 Status），避免无效构建消耗资源。

#### 公私仓库兼容方案

| 场景 | 处理方式 |
|------|----------|
| 公开仓库 PR | Token 需有 `repo:status` 或 `public_repo` 范围；`list_open_prs` 对公开仓库无需额外权限 |
| 私有仓库 PR | Token 需有 `repo`（完整读写）范围，或 Fine-grained PAT 对目标私有仓库显式授权 |
| `WATCH_REPOS` 混合公私 | 同一 Token 只要权限覆盖所有列出的仓库即可；若某仓库 403，Poller 会记录错误并跳过，不影响其他仓库 |
| repo manifest 包含私有子仓库 | `RepoWorkspace.bootstrap` 使用带 Token 的 HTTPS URL clone manifest；子仓库也用同样鉴权 URL |

**Token 建议**：使用 GitHub Fine-grained PAT，对每个需要监听的仓库分别授予：
- `Contents: Read`（clone/fetch）
- `Commit statuses: Read & Write`（写 Commit Status）
- `Pull requests: Read & Write`（读 PR 列表、写评论）

### 9.6 日志存储
- [x] 当前阶段：日志写入本机 `LOG_DIR`（默认 `./logs/{job_id}.log`）。
- [x] PR 评论里附日志末尾 30 行摘要，开发者可通过评论快速判断失败原因。
- [ ] 如需远程可访问（开发者自助查看完整日志），后续可在机器上起简单 HTTP 静态文件服务（如 `python3 -m http.server`），在评论里附链接。本次暂不实现。

### 9.7 Checks API — 背景说明与决策

#### 什么是 Checks API？

GitHub 有两套「把 CI 结果写回 PR」的机制：

| | Commit Status API（当前使用） | Checks API（GitHub Actions 使用） |
|---|---|---|
| PR 页面显示 | 小彩色圆点（pending/success/failure） | 完整 Check Run 卡片，有标题、步骤、摘要 |
| 日志展示 | ❌ 无 | ✅ 日志内嵌在 GitHub 页面里可直接看 |
| 代码行注解 | ❌ 无 | ✅ 可在具体代码行上标注错误（Annotations） |
| 手动重跑按钮 | ❌ 无 | ✅ GitHub UI 上有「Re-run」按钮 |
| 富文本构建报告 | ❌ 无 | ✅ Summary 支持 Markdown 表格/图表 |
| 所需权限 | PAT `repo:status` | GitHub App 安装 token，或 Fine-grained PAT `checks:write` |
| 复杂度 | 简单，直接调 REST | 需要创建 GitHub App，管理 installation token（有效期 1h，需定时刷新） |

#### 视觉对比

```
Commit Status（当前）：
PR 页面底部：  ● RISC-V Native CI — Build succeeded.  [Details]
                                        ↑ 只有这一行，点 Details 跳到你配置的 URL

Checks API：
PR 页面 Checks 标签：
  ┌─────────────────────────────────────────┐
  │ ✅ RISC-V Native CI    342s             │
  │   ├ Step 1/4: fetch-deps    ✅ 12s      │
  │   ├ Step 2/4: build-kernel  ✅ 280s     │
  │   ├ Step 3/4: run-tests     ✅ 45s      │
  │   └ Step 4/4: package       ✅ 5s       │
  │                                         │
  │ Summary: Built on deb1, 0 warnings      │
  │ [Re-run]  [View logs]                   │
  └─────────────────────────────────────────┘
```

#### 决策

- [x] **近期（Phase 0~4）维持 Commit Status API**，配合 PR 评论贴日志摘要，体验已经够用。
- [ ] **Phase 6（可选，未来）**：若有需要升级到 Checks API，需先创建 GitHub App（在 GitHub Settings → Developer settings → GitHub Apps），获取 App ID 和 Private Key，程序里定时用 JWT 换取 installation token（1 小时有效）。届时新增 `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY` 配置项，Reporter 自动选择用哪套 API。

### 9.8 `/rerun` 支持
- [ ] 暂列为 Phase 6 低优先级功能。实现方案：轮询 PR 评论（`list_issue_comments`），发现最新一条内容为 `/rerun` 的评论（且作者在白名单/是协作者）时，重新入队当前 `head_sha`，并回复「已加入队列 #N」。

---

## 10 Phase 0 实施清单（可立即执行）

Phase 0 不涉及架构改动，只修复最紧迫的红叉问题：

- [ ] 在 Runner 代码里：修改 `executor.py` 第 14 行 `WORKFLOW_REL` 为 `os.path.join(".riscv", "workflow.yml")`
- [ ] 在目标仓库里：删除 `.github/workflows/riscv-ci.yml`，新建 `.riscv/workflow.yml`（内容相同）
- [ ] 验证：提交新 PR，确认 GitHub Actions 标签页不再报错，Commit Status 正常显示

---

*文档最后更新：2026-03-25（增补 §8 安全与执行注意事项；§8/§9/§10 章节编号顺延）*
