# RISC-V Lite Runner 部署说明

## 前置条件

- Python 3.x、`git`、网络可访问 `api.github.com` 与 `github.com`
- 已安装依赖：`pip install -r requirements.txt`（建议使用 venv）
- **repo 多仓模式**另需安装 Google `repo` 工具（`WORKSPACE_MODE=repo` 时**必须**在 PATH 中能找到 `repo` 命令；未安装会报错并提示安装方式）：
  - Debian/Ubuntu：`sudo apt install repo`
  - 或下载脚本：`mkdir -p ~/.bin && curl -fsSL https://storage.googleapis.com/git-repo-downloads/repo -o ~/.bin/repo && chmod +x ~/.bin/repo`，并把 `~/.bin` 加入 `PATH`（systemd 服务需在 `Environment=PATH=...` 中包含该路径）

## 配置 `.env`

1. 在**项目根目录**（与 `core/` 同级）创建 `.env`，可参考 `.env.example`。
2. **单仓库（默认）**：`GITHUB_TOKEN`、`GITHUB_REPO`、`TARGET_BRANCH`；可选 `WORKSPACE_DIR`、`POLL_INTERVAL`、`STEP_TIMEOUT` 等。
3. **repo 多仓模式**：设置 `WORKSPACE_MODE=repo`、`MANIFEST_REPO`、`TARGET_BRANCH`；`GITHUB_REPO` 可不填。`MANIFEST_REPO` 填 **`owner/repo`**（如 `TheHotSummery/manifest`），**不要**写成 `owner/repo.git`；程序会自动去掉误写的 `.git`，否则 clone 地址会变成 `…/repo.git.git` 而失败。`MANIFEST_FILE` 须与 manifest 仓库里实际 XML 文件名一致（如 `default.xml` 或 `spacemit.xml`）。`WATCH_REPOS` 留空时，首次 `repo sync` 后会从 manifest 解析出所有子仓库并轮询 PR。
4. **`MANIFEST_GITHUB_ORG`（可选）**：manifest 里 `<remote fetch="../某组织">` 这类相对路径时，程序会推断 GitHub `owner` 以拼出 `owner/repo`。若推断不准，可显式设置，例如 `MANIFEST_GITHUB_ORG=spacemit-robotics`。
5. **工作流文件**：Runner 读取 `.riscv/workflow.yml`（勿放在 `.github/workflows/`，以免被 GitHub Actions 误解析）。**单仓模式**：路径为 clone 根下的 `.riscv/workflow.yml`。**repo 多仓模式**：**优先**使用「当前 PR 所在子仓库」下的该文件；若子仓没有，再尝试 `WORKSPACE_DIR` 根目录（共用）。若 **PR 分支没有**该文件而 **`TARGET_BRANCH`（如 main）上有**，Runner 会在 `sync_for_pr` 之后从 `origin/<TARGET_BRANCH>` **单独检出** `.riscv/workflow.yml`，其余代码仍为 PR 的 `head_sha`。请保证 `.env` 里 `TARGET_BRANCH` 与 PR 的 **base 分支**一致。
6. **注意**：systemd 的 `EnvironmentFile` 要求 `KEY=value` 一行一项，**不要**写 `export`。

### 单仓库示例

```ini
GITHUB_TOKEN=ghp_xxxxxxxx
GITHUB_REPO=owner/repo
TARGET_BRANCH=main
POLL_INTERVAL=15
WORKSPACE_DIR=./workspace
STEP_TIMEOUT=3600
```

### repo 多仓示例

```ini
GITHUB_TOKEN=ghp_xxxxxxxx
TARGET_BRANCH=main
WORKSPACE_MODE=repo
MANIFEST_REPO=spacemit-robotics/manifest
MANIFEST_BRANCH=main
MANIFEST_FILE=default.xml
MANIFEST_GITHUB_ORG=spacemit-robotics
WORKSPACE_DIR=./workspace-repo
POLL_INTERVAL=15
# 只测少数子仓时可写 WATCH_REPOS=spacemit-robotics/build,spacemit-robotics/scripts
```

## 安装 systemd 服务

1. 将 `riscv-runner.service` 复制到 `/etc/systemd/system/`，并替换占位符：
   - `<YOUR_USER>`：运行服务的 Linux 用户
   - `<PATH_TO_RISCV_LITE_RUNNER>`：项目根目录的**绝对路径**（与 `core/`、`run.sh` 同级）
2. 确保该用户对项目目录、`.env`、`WORKSPACE_DIR` 有读写权限。
3. 重载并启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now riscv-runner.service
```

4. 查看状态与日志：

```bash
sudo systemctl status riscv-runner.service
journalctl -u riscv-runner.service -f
```

## 手动前台运行（调试用）

在项目根目录：

```bash
set -a && source .env && set +a
python3 -m core.main
```

或使用 `./run.sh`（需已 `export` 环境变量或配合 `env` 注入）。

## 工作流里的 `sudo apt`（自托管常见）

Runner 执行 `run` 时**无交互终端**，`sudo` 若需输入密码会失败（`A terminal is required to authenticate`）。

- **不要把密码写进仓库或 `.env`**。
- **推荐**：在板子/Runner 上**事先装好**编译依赖（`apt` 一次装好，或由镜像提供）。
- **可选**：为运行服务的用户配置 **免密 sudo**（仅允许必要命令），例如：

```text
# /etc/sudoers.d/riscv-runner（用 visudo 编辑）
bianbu ALL=(ALL) NOPASSWD: /usr/bin/apt-get, /usr/bin/apt
```

路径以 `which apt-get` 为准；范围尽量收窄。

程序默认还会对工作流里 **含 `sudo` 的步骤** 做检测：若环境变量 **`SKIP_SUDO_STEPS`** 为真（默认），则**不执行**该步 shell，仅在日志中说明原因（避免无 TTY 时 sudo 直接失败）。需要强制执行时请设 `SKIP_SUDO_STEPS=false` 并自行保证免密 sudo 等。

## repo 模式：工作区损坏时

若曾手动删除过 `WORKSPACE_DIR` 下某些 `.git`、`.repo/manifests` 等，`repo sync` 可能报 `manifest.xml` 不存在、`unparseable HEAD` 等。Runner 会在**首次** `repo sync` 失败时自动删除 `.repo` 并重新 `repo init` + `repo sync` 一次。若仍失败，请**整目录清空**后重启（先备份工作区根目录的 `.riscv/workflow.yml` 等自建文件）：

```bash
rm -rf /path/to/WORKSPACE_DIR
```
