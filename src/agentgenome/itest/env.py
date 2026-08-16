"""集成测试环境的生命周期:拉起、构建闭包、无条件销毁。

## 环境是牛不是宠物

跑完无论成败一律 `down -v`,销毁写在 `finally` 里。**这里没有 `--keep-env` 这类开关,
也不要加。** 一旦有了它,"失败时保留环境方便排查"会变成默认操作,然后并发任务开始互相
踩,然后磁盘满。排查现场的需求由报告里的复现命令满足(见 `itest.report`),那个答案同时
也更适合给人用——它是一行可以直接粘贴执行的命令,而不是一台需要你先 ssh 上去的机器。

## 三条硬要求

- **`up -d --wait`。** 不等健康就开跑会产生大量假失败,而假失败比漏测更毒:它让团队
  不再信任这道门禁。
- **每任务独立的 compose 项目名。** 这是多任务并行的前提。共用项目名的话第二个任务会
  直接接管第一个的容器。
- **只构建闭包内的模块。** 从这次改动涉及的模块出发沿项目地图 `depends_on` 求闭包。
  一次集成测试不必重建整个系统。

## 超时之后销毁用的是新额度

共用一个 deadline 的话,超时那一刻销毁立刻也超时,于是容器泄漏——而超时恰恰是最需要
销毁真的生效的时候。

## 这个模块被什么验证,又没被什么验证

单元测试用一个**假的 `docker` 可执行文件**跑真子进程,验的是上面这些编排逻辑:命令怎么
拼、闭包怎么算、顺序对不对、异常与超时路径下销毁跑没跑。

docker 自己的行为——`--wait` 到底等不等健康、并发项目会不会撞端口、`down -v` 清不清
干净——**一条都没被这里的测试覆盖**,那是 docker 的事实而不是我们的逻辑,由一次人工
spike 负责(`.scratch/07-integration-testing/issues/01`)。
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from agentgenome.config import INITIAL_TIMEOUT_SECONDS
from agentgenome.genome.models import ProjectMap

#: 编排文件的约定位置。集成测试是跨模块的事,所以在 Workspace 顶层。
COMPOSE_FILE = Path("itest") / "compose.yaml"

#: compose 项目名的前缀。裸用任务号的话,同一台机器上的别的 compose 项目可能撞上。
PROJECT_PREFIX = "ag"

#: 销毁自己的时限。与整体超时分开——见模块说明。
TEARDOWN_TIMEOUT_S = 120

_ILLEGAL = re.compile(r"[^a-z0-9_-]+")


class EnvUnavailable(RuntimeError):
    """环境起不来。**这是环境类失败,不是代码问题。**

    编排文件缺失、镜像构建失败、健康检查没过——改业务代码解决不了,每一轮都会以完全
    相同的方式失败。该升级人工,不该进修复循环。
    """


class EnvTimeout(EnvUnavailable):
    """整体超时。卡住的环境不能永远占着资源。"""


@dataclass(frozen=True)
class EnvPlan:
    """这次要拉起什么。**纯数据**,由纯函数算出来,可以单独断言。"""

    project: str
    compose_file: Path
    #: 构建闭包内的模块 id。服务名按约定与模块 id 同名。
    modules: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "compose_file": str(self.compose_file),
            "modules": list(self.modules),
        }


@dataclass(frozen=True)
class CommandResult:
    """在环境里跑一条命令的结果。"""

    argv: tuple[str, ...]
    exit_code: int
    output: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def build_closure(modules: Sequence[str], project_map: ProjectMap) -> list[str]:
    """从涉及模块出发,沿 `depends_on` 求依赖闭包。

    **必须防环。** 项目地图是员工产出的,循环依赖是迟早的事;这里死循环的话整套集成测试
    挂死在一个本该几毫秒的纯计算上。

    地图上没有的模块 id 直接丢掉:编造的 id 不该让整个集成测试炸掉,它只是没有对应的
    服务可以构建。
    """
    known = {module.id: module for module in project_map.modules}
    found: set[str] = set()
    pending = [module_id for module_id in modules if module_id in known]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending += [dep for dep in known[current].depends_on if dep in known and dep not in found]
    return sorted(found)


def compose_project(task_id: str) -> str:
    """这个任务的 compose 项目名。

    compose 只接受小写字母、数字与 `-`/`_`,且必须以字母数字开头。任务号本身是合规的,
    但**不要假设**——一个被拒绝的项目名会让整套集成测试在最外层炸掉,而错误信息看起来
    像 docker 坏了。
    """
    cleaned = _ILLEGAL.sub("-", task_id.lower()).strip("-_")
    return f"{PROJECT_PREFIX}-{cleaned or 'task'}"


def plan_environment(
    workdir: Path,
    task_id: str,
    modules: Sequence[str],
    project_map: ProjectMap,
    compose_file: Path | None = None,
) -> EnvPlan:
    """算出这次要拉起什么。编排文件不在就是环境不可用。

    **纯到底为止**:除了确认编排文件存在,这里不碰任何外部状态。
    """
    relative = compose_file or COMPOSE_FILE
    path = Path(workdir) / relative
    if not path.is_file():
        raise EnvUnavailable(
            f"集成测试编排文件不存在: {relative}。"
            "这是项目自己的资产,系统不会替你生成——请为这个项目补一份。"
        )
    return EnvPlan(
        project=compose_project(task_id),
        compose_file=path,
        modules=tuple(build_closure(modules, project_map)),
    )


@dataclass
class _Deadline:
    """还剩多少时间。超时那一刻要能把子进程真的杀掉。"""

    total_s: float | None
    started: float = field(default_factory=time.monotonic)

    def remaining(self) -> float | None:
        if self.total_s is None:
            return None
        return max(0.0, self.total_s - (time.monotonic() - self.started))


def _deadline_seconds(timeout_s: int) -> float | None:
    """把新项目的“近似无限”初值翻译成 subprocess 的无限等待。

    ``subprocess`` 最终会把秒数换成毫秒传给系统的 poll API；一亿秒会在部分平台
    溢出。配置层仍保留可展示、可编辑的整数，只有执行边界把这一个哨兵值转成 ``None``。
    """
    return None if timeout_s >= INITIAL_TIMEOUT_SECONDS else float(timeout_s)


class ComposeEnvironment:
    """拉起一套环境,用完销毁。

    用作上下文管理器。`__enter__` 构建闭包并 `up -d --wait`,`__exit__` 无条件
    `down -v`——body 抛异常也销毁,超时也销毁。
    """

    def __init__(
        self,
        workdir: Path,
        plan: EnvPlan,
        timeout_s: int,
        docker: str = "docker",
    ) -> None:
        self.workdir = Path(workdir)
        self.plan = plan
        self.timeout_s = timeout_s
        #: 可执行文件名。测试指向一个假的 docker——换掉的只是"那个进程是谁",
        #: 子进程、超时、销毁顺序全都是真的。
        self.docker = docker
        self._deadline = _Deadline(_deadline_seconds(timeout_s))
        self._started = False

    # --- 生命周期 -------------------------------------------------------------

    def __enter__(self) -> ComposeEnvironment:
        # **`__enter__` 里的失败要自己销毁。** Python 只在 `__enter__` 成功返回之后才会
        # 调 `__exit__`,所以"起环境的过程中挂了"这条路上没有任何人替它收尸——而那正是
        # 容器最可能已经建出来、并且已经占住端口的时刻。
        self._started = True
        try:
            if self.plan.modules:
                self._compose(["build", *self.plan.modules], what="构建镜像")
            self._compose(["up", "-d", "--wait"], what="拉起环境")
        except BaseException:
            self._teardown()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # 不返回 True:销毁失败不该把 body 的异常吞掉。
        self._teardown()

    def _teardown(self) -> None:
        """销毁。幂等——`__enter__` 里已经销毁过的话 `__exit__` 不该再来一次。"""
        if not self._started:
            return
        self._started = False
        # 销毁用**新的**额度。共用已经耗尽的那个的话,超时那一刻销毁立刻也超时,
        # 容器就此泄漏——而超时恰恰是最需要销毁真的生效的时候。
        self._deadline = _Deadline(float(TEARDOWN_TIMEOUT_S))
        self._run(["down", "-v"], check=False)

    # --- 在环境里干活 ---------------------------------------------------------

    def run(self, service: str, argv: Sequence[str]) -> CommandResult:
        """在某个服务里跑一条命令。

        **失败不抛异常。** 用例挂掉是集成测试的正常输出,抛出去的话报告就写不出来了,
        而报告正是这一步唯一有价值的产物。
        """
        return self._run(["run", "--rm", "-T", service, *argv], check=False)

    def seed(self, service: str, argv: Sequence[str]) -> None:
        """灌测试数据。**失败即环境不可用。**

        没灌上就开跑,失败会以"业务逻辑不对"的样子出现——而实际原因是库里没数据。这种
        误导会让开发员工去改一段本来没问题的代码,而且每一轮都改不好。
        """
        result = self._run(["run", "--rm", "-T", service, *argv], check=False)
        if not result.ok:
            raise EnvUnavailable(
                f"灌测试数据失败(退出码 {result.exit_code}): {result.output[-2000:]}"
            )

    def services(self) -> list[str]:
        """编排文件里声明的**全部**服务。

        不是构建闭包——闭包只有"这次改了什么",而集成测试挂掉时最需要看的往往正是没被改过
        的那些:数据库、消息队列、网关。只收闭包的日志等于在最需要材料的时候没有材料。
        """
        result = self._run(["config", "--services"], check=False)
        return sorted(line.strip() for line in result.output.splitlines() if line.strip())

    def logs(self, tail: int | None = None) -> dict[str, str]:
        """逐个服务收日志。收不到的服务不出现在结果里,不要用空串冒充。"""
        found = {}
        for service in self.services():
            argv = ["logs", "--no-color"]
            if tail is not None:
                argv += ["--tail", str(tail)]
            result = self._run([*argv, service], check=False)
            if result.ok:
                found[service] = result.output
        return found

    # --- 命令 -----------------------------------------------------------------

    def argv(self, args: Sequence[str]) -> list[str]:
        """拼出完整命令。**纯函数**,可以单独断言。

        `-p` 每条命令都要带:漏一次的表现是那条命令作用在默认项目上,而并发任务于是
        开始互相踩——症状离原因非常远。
        """
        return [
            self.docker,
            "compose",
            "-p",
            self.plan.project,
            "-f",
            str(self.plan.compose_file),
            *args,
        ]

    def _compose(self, args: Sequence[str], what: str) -> CommandResult:
        result = self._run(args, check=False)
        if not result.ok:
            raise EnvUnavailable(f"{what}失败(退出码 {result.exit_code}): {result.output[-2000:]}")
        return result

    def _run(self, args: Sequence[str], check: bool) -> CommandResult:
        argv = self.argv(args)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=self._deadline.remaining(),
                check=check,
            )
        except subprocess.TimeoutExpired as error:
            # `subprocess.run` 超时会把子进程杀掉再抛,所以这里不必自己收尸。
            raise EnvTimeout(
                f"集成测试环境超过 {self.timeout_s} 秒未完成: {' '.join(args)}"
            ) from error
        return CommandResult(
            argv=tuple(argv),
            exit_code=completed.returncode,
            output=(completed.stdout or "") + (completed.stderr or ""),
            duration_s=time.monotonic() - started,
        )


__all__ = [
    "COMPOSE_FILE",
    "PROJECT_PREFIX",
    "TEARDOWN_TIMEOUT_S",
    "CommandResult",
    "ComposeEnvironment",
    "EnvPlan",
    "EnvTimeout",
    "EnvUnavailable",
    "build_closure",
    "compose_project",
    "plan_environment",
]
