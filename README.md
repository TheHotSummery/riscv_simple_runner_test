# RISC-V Action Runner

在 **自托管 RISC-V（或其它 Linux）机器** 上运行的轻量 CI：轮询 GitHub 上指向目标分支的 **Pull Request**，按 PR 的 **head 提交** 同步代码、执行自定义工作流，并把结果写回 **Commit Status** 与（可选）**PR 评论**。

与 GitHub Actions 官方 Runner **不共用协议**；工作流文件使用仓库内的 **`.riscv/workflow.yml`**（自定义 YAML），**不要**放在 `.github/workflows/`，以免被 GitHub 当成 Actions 工作流解析。

---

## 功能概览

| 能力 | 说明 |
|------|------|
| 触发方式 | 定时轮询 GitHub API（可配合 ETag 减少请求） |
| 单仓库 / 多仓 `repo` | `WORKSPACE_MODE=git` 或 `repo` + manifest |
| 队列 | 按子仓库分槽；构建未完成时不会重复入队同一 SHA |
| 状态上报 | Commit Status；可选 PR 评论（入队 / 进度 / 日志摘要） |
| 工作流 | 顺序执行 `steps`，每步 `run` 为 shell，单步超时与进程组清理 |
| 安全相关 | 可选作者白名单；含 `sudo` 的步骤可默认跳过（见下文） |

更完整的设计与注意事项见 [DESIGN.md](DESIGN.md)；部署与 systemd 见 [DEPLOY.md](DEPLOY.md)。

---

## 环境要求

### Runner 机器

- Python 3.x、`git`、可访问 `api.github.com` 与 `github.com`
- 依赖：`pip install -r requirements.txt`（建议使用 venv）

### 建议提前装好的构建环境（避免在 CI 里 `sudo apt` 失败）

自托管子进程 **无交互 TTY**，`sudo` 默认会要求密码并失败。推荐在机器上 **预先安装** 编译依赖，例如：

```bash
sudo apt update
sudo apt install -y git build-essential cmake ninja-build
```

**repo 多仓模式**还需安装 Google 的 **`repo` 工具**（`apt install repo` 或 [官方安装方式](https://source.android.com/docs/setup/download)），并保证 `which repo` 有输出。

### GitHub Token

使用 **Fine-grained PAT** 或 Classic PAT，对目标仓库至少需要：

- 读仓库、读 PR、写 Commit Status、（若开启 PR 评论）写 Issue/PR 评论  

具体见 [DEPLOY.md](DEPLOY.md) 中的说明。

---

## 快速开始

```bash
cd riscv-action-runner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env：至少填写 GITHUB_TOKEN、TARGET_BRANCH；单仓模式填写 GITHUB_REPO

set -a && source .env && set +a
python3 -m core.main
```

或使用项目根目录的 `main.py`（需已配置环境变量）。

---

## 配置要点（`.env`）

| 变量 | 说明 |
|------|------|
| `GITHUB_TOKEN` | 必填 |
| `TARGET_BRANCH` | PR 的 **base 分支**（须与 GitHub 上 PR 一致，如 `main`） |
| `GITHUB_REPO` | 单仓模式必填：`owner/repo` |
| `WORKSPACE_MODE` | `git`（默认）或 `repo` |
| `WORKSPACE_DIR` | 工作区根目录，默认 `./workspace` |
| `POLL_INTERVAL` | 轮询间隔（秒），默认 `15` |
| `STEP_TIMEOUT` | 单步最大执行时间（秒），默认 `3600` |
| `WORKFLOW_DIR` | **最高优先级**：指定一个目录，该目录下必须有 `.riscv/workflow.yml`；所有 PR 的构建都在此目录下执行（shell `cwd` = 该目录）。典型用途：repo 多仓统一构建，指向 `build/` 子仓目录（含 `envsetup.sh`、`build.sh`），例如 `WORKFLOW_DIR=./workspace/build`。留空 = 按自动查找逻辑 |
| `SKIP_SUDO_STEPS` | 默认 `true`：若某步 `run` 中含 `sudo`，则**跳过执行**该步并写说明（避免无 TTY）；设为 `false` 可强制执行（需免密 sudo 等） |
| `MANIFEST_*` / `WATCH_REPOS` / `MANIFEST_GITHUB_ORG` | 仅 `WORKSPACE_MODE=repo` 时使用，见 [DEPLOY.md](DEPLOY.md) |

完整列表以 `.env.example` 与 [DEPLOY.md](DEPLOY.md) 为准。

---

## 工作流文件放哪里

| 优先级 | 条件 | 说明 |
|--------|------|------|
| **1（最高）** | 设置了 `WORKFLOW_DIR` | 直接用该目录下的 `.riscv/workflow.yml`，所有 PR 共用，shell `cwd` = 该目录。**repo 统一构建推荐此方式**（指向 `./workspace/build`） |
| 2 | 当前 PR 所在子仓库内有该文件 | PR 分支检出后，子仓根 `.riscv/workflow.yml` 存在 |
| 3 | PR 分支没有，但 base 分支有 | 自动从 `origin/<TARGET_BRANCH>` 只检出该文件，代码仍为 PR head |
| 4 | 工作区根目录有 | `WORKSPACE_DIR/.riscv/workflow.yml`（所有子仓共用） |
| 5 | 都没有 | 报错 |

文件名固定为 **`.riscv/workflow.yml`**（相对上述「工作目录」根）。

---

## `.riscv/workflow.yml` 写法说明

本 Runner **不是** GitHub Actions 语法；根节点为 **`steps`**，为**列表**，每一项至少包含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | 字符串 | 步骤名称（展示在日志与进度里） |
| `run` | 字符串 | **一段 shell 脚本**（支持 `\|` 多行）；在对应工作目录下执行，`cwd` 为上面「工作流文件所在项目的根」 |

执行规则：

1. 按列表 **从上到下** 依次执行。
2. 任一步 **`run` 退出码非 0**，整次构建失败，后续步骤不再执行。
3. 单步超过 `STEP_TIMEOUT` 会终止该步的**整个进程组**。
4. 若 `SKIP_SUDO_STEPS=true`（默认）且该步 `run` 中出现独立单词 **`sudo`**，则**不执行**该步 shell，在日志中输出说明，该步视为成功（详见 `core/executor.py`）。

### 模板文件

仓库内提供 **[`.riscv/workflow.yml.example`](.riscv/workflow.yml.example)**，可复制到目标仓库并重命名为 `.riscv/workflow.yml` 后修改。模板中已注释：

- 为何不要放到 `.github/workflows/`；
- 单仓 / repo 多仓路径习惯；
- **不要在 CI 里依赖交互式 `sudo`**，应预装或使用免密 sudo；
- 示例步骤：检查环境、检查 `cmake`/`ninja`、清理 `build/`、`cmake` + `ninja` 编译与测试。

可按项目需要增删 `steps`，只要保持 **`name` + `run`** 结构即可。

---

## 与目标仓库的约定

1. **删除或迁出** 误放在 `.github/workflows/` 下、会被 GitHub Actions 当官方工作流解析的旧文件（若存在），避免与自建 Runner 双重状态。
2. 贡献者若只在 **base 分支**（如 `main`）上有 `.riscv/workflow.yml`，而 **PR 分支没有**，Runner 会尝试从 **`origin/<TARGET_BRANCH>`** 单独检出该文件（其余代码仍为 PR 的 `head`）。
3. 敏感操作、任意 shell 均 **信任工作流编写者**；生产环境建议配合分支保护与作者白名单等，见 [DESIGN.md](DESIGN.md) 安全章节。

---

## 文档索引

| 文档 | 内容 |
|------|------|
| [DEPLOY.md](DEPLOY.md) | systemd、repo/`sudo`、工作区损坏处理等 |
| [DESIGN.md](DESIGN.md) | 架构、模块、安全与执行注意事项 |

---

## 许可证

若仓库未单独声明许可证，以仓库根目录许可证文件为准。
