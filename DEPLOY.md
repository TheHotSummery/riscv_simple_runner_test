# RISC-V Lite Runner 部署说明

## 前置条件

- Python 3.x、`git`、网络可访问 `api.github.com` 与 `github.com`
- 已安装依赖：`pip install -r requirements.txt`（建议使用 venv）

## 配置 `.env`

1. 在**项目根目录**（与 `core/` 同级）创建 `.env`，可参考 `.env.example`。
2. 至少设置：`GITHUB_TOKEN`、`GITHUB_REPO`、`TARGET_BRANCH`；按需设置 `WORKSPACE_DIR`、`POLL_INTERVAL`、`STEP_TIMEOUT`。
3. **注意**：systemd 的 `EnvironmentFile` 要求 `KEY=value` 一行一项，**不要**写 `export`。

示例：

```ini
GITHUB_TOKEN=ghp_xxxxxxxx
GITHUB_REPO=owner/repo
TARGET_BRANCH=main
POLL_INTERVAL=15
WORKSPACE_DIR=./workspace
STEP_TIMEOUT=3600
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
