"""工作区抽象接口：单仓库（git）与多仓库（repo manifest）共用同一套调度入口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PRInfo:
    """描述一个需要构建的 PR 的必要信息。"""

    repo: str        # GitHub 仓库全名，如 "org/sub-repo"
    pr_number: int
    head_sha: str    # PR 当前 head commit SHA


class WorkspaceBase(ABC):
    """
    工作区基类。

    子类职责：
      - bootstrap()     首次初始化（clone / repo init+sync）
      - sync_for_pr()   将工作区调整到「可构建此 PR」的状态
      - workflow_dir()  返回 executor 用来查找 .riscv/workflow.yml 的目录
    """

    @abstractmethod
    def bootstrap(self) -> None:
        """
        首次初始化工作区。
        git 模式：若 .git 不存在则 clone。
        repo 模式：若 .repo 不存在则 repo init + repo sync（全量）。
        进程重启后再次调用时应只做增量操作（幂等）。
        """

    @abstractmethod
    def sync_for_pr(self, pr: PRInfo) -> None:
        """
        将工作区同步到可以构建 pr 的状态。
        git 模式：git fetch origin + git reset --hard head_sha。
        repo 模式：repo sync（增量）+ 目标子仓库 git reset --hard head_sha。
        失败时抛出 RuntimeError 或 OSError。
        """

    def clean_artifacts(self) -> None:
        """
        清理编译产物（保留源码 / 依赖 cache）。
        默认空实现；可由 workflow.yml 的 clean step 处理，
        也可在子类中覆盖以执行 make clean 等。
        """

    @abstractmethod
    def workflow_dir(self) -> str:
        """
        返回多仓工作区「树根」或单仓 clone 根目录。
        executor 默认在此目录下查找 .riscv/workflow.yml；repo 模式还可使用 workflow_dir_for_pr。
        """

    def workflow_dir_for_pr(self, pr: PRInfo) -> str:
        """
        执行某个 PR 的构建时，在哪个目录下查找 .riscv/workflow.yml（并作为 shell 的 cwd）。
        单仓模式：与 workflow_dir() 相同。
        多仓模式：优先使用「该 PR 所在子仓库」内的 .riscv/workflow.yml（见 RepoWorkspace）。
        """
        return self.workflow_dir()
