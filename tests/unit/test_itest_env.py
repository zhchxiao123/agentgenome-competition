"""集成测试环境的生命周期。

**覆盖边界。** 这一组用一个假的 `docker` 可执行文件跑真子进程,验的是**我们的编排逻辑**:
命令怎么拼、构建闭包怎么算、up→跑→`finally` down 的顺序、超时强杀、编排文件缺失怎么降级。

docker **自己**的行为——`--wait` 到底等不等健康、并发项目会不会撞端口、`down -v` 清不清
干净——这里一条都没验,也验不了。那三条由 `.scratch/07-integration-testing/issues/01` 的
人工 spike 覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentgenome.config import INITIAL_TIMEOUT_SECONDS
from agentgenome.genome.models import ProjectMap
from agentgenome.itest.env import (
    ComposeEnvironment,
    EnvTimeout,
    EnvUnavailable,
    build_closure,
    compose_project,
    plan_environment,
)
from tests.fixtures.fake_docker import FakeDocker, install_fake_docker
from tests.fixtures.tree import write_flat_as_tree

MAP = ProjectMap.model_validate(
    {
        "version": 1,
        "project": {"name": "mall"},
        "modules": [
            {
                "id": "order-service",
                "path": "repos/order-service/",
                "depends_on": ["inventory-service"],
            },
            {
                "id": "inventory-service",
                "path": "repos/inventory-service/",
                "depends_on": ["shared-db"],
            },
            {"id": "shared-db", "path": "repos/billing-service/"},
            {"id": "gateway", "path": "repos/gateway/", "depends_on": ["order-service"]},
        ],
    }
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "itest").mkdir()
    (tmp_path / "itest" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def docker(tmp_path: Path) -> FakeDocker:
    return install_fake_docker(tmp_path / "bin")


# --- 构建闭包(纯函数)------------------------------------------------------


def test_the_closure_pulls_in_transitive_dependencies() -> None:
    """只构建直接依赖是不够的:order 依赖 inventory,而 inventory 依赖 shared-db。"""
    assert build_closure(["order-service"], MAP) == [
        "inventory-service",
        "order-service",
        "shared-db",
    ]


def test_modules_nothing_depends_on_stay_out() -> None:
    """一次集成测试不必重建整个系统——那正是"只构建受影响闭包"要省下的东西。"""
    assert "gateway" not in build_closure(["order-service"], MAP)


def test_the_closure_survives_a_dependency_cycle() -> None:
    """地图是员工产出的,循环依赖是迟早的事。这里死循环的话整套集成测试挂死。"""
    cyclic = ProjectMap.model_validate(
        {
            "version": 1,
            "project": {"name": "loop"},
            "modules": [
                {"id": "a", "path": "repos/order-service/", "depends_on": ["b"]},
                {"id": "b", "path": "repos/inventory-service/", "depends_on": ["a"]},
            ],
        }
    )

    assert build_closure(["a"], cyclic) == ["a", "b"]


def test_a_module_that_is_not_on_the_map_is_dropped() -> None:
    """编造的模块 id 不该让整个集成测试炸掉——它只是没有对应的服务可构建。"""
    assert build_closure(["order-service", "made-up"], MAP) == [
        "inventory-service",
        "order-service",
        "shared-db",
    ]


def test_an_empty_input_yields_an_empty_closure() -> None:
    assert build_closure([], MAP) == []


# --- compose 项目名 ---------------------------------------------------------


def test_each_task_gets_its_own_compose_project() -> None:
    """并发跑多个任务的前提。共用项目名的话第二个任务会接管第一个的容器。"""
    assert compose_project("ag-20260808-001") != compose_project("ag-20260808-002")


def test_the_project_name_is_stable_for_a_task() -> None:
    """崩溃恢复后要能找回同一套容器去销毁,而不是留下一堆孤儿。"""
    assert compose_project("ag-20260808-001") == compose_project("ag-20260808-001")


def test_the_project_name_is_a_legal_compose_project_name() -> None:
    """compose 只收小写字母、数字与 `-`/`_`。任务号里的连字符没问题,大写会被拒。"""
    name = compose_project("AG-20260808-001")

    assert name == name.lower()
    assert all(char.isalnum() or char in "-_" for char in name)
    assert name[0].isalnum()


# --- 编排文件缺失 -----------------------------------------------------------


def test_a_missing_compose_file_is_an_environment_problem(tmp_path: Path) -> None:
    """让 AI 去修一个它修不了的东西只会白烧三轮 token。这条要走环境类失败升级人工。"""
    with pytest.raises(EnvUnavailable) as error:
        plan_environment(tmp_path, "ag-1", ["order-service"], MAP)

    assert "compose" in str(error.value)


def test_the_compose_file_location_can_be_overridden(tmp_path: Path) -> None:
    """项目可以把编排文件放在别处。"""
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "itest.yaml").write_text("services: {}\n", encoding="utf-8")

    plan = plan_environment(
        tmp_path, "ag-1", ["order-service"], MAP, compose_file=Path("ops/itest.yaml")
    )

    assert plan.compose_file.name == "itest.yaml"


# --- 生命周期(假 docker,真子进程)----------------------------------------


def _plan(workspace: Path, modules: list[str] | None = None):
    return plan_environment(workspace, "ag-1", modules or ["order-service"], MAP)


def test_the_happy_path_builds_starts_and_tears_down(workspace: Path, docker: FakeDocker) -> None:
    with ComposeEnvironment(workspace, _plan(workspace), timeout_s=30, docker=docker.command):
        pass

    assert docker.subcommands() == ["build", "up", "down"]


def test_the_initial_practically_unlimited_timeout_is_safe_for_subprocesses(
    workspace: Path, docker: FakeDocker
) -> None:
    """新项目的宽松初值不能超过操作系统等待 API 能接受的范围。"""
    with ComposeEnvironment(
        workspace,
        _plan(workspace),
        timeout_s=INITIAL_TIMEOUT_SECONDS,
        docker=docker.command,
    ):
        pass

    assert docker.subcommands() == ["build", "up", "down"]


def test_starting_waits_for_health(workspace: Path, docker: FakeDocker) -> None:
    """不等健康就跑测试会产生大量假失败,而假失败比漏测更毒:它让人不再信任门禁。"""
    with ComposeEnvironment(workspace, _plan(workspace), timeout_s=30, docker=docker.command):
        pass

    up = next(call for call in docker.calls() if "up" in call)
    assert "--wait" in up


def test_only_the_closure_is_built(workspace: Path, docker: FakeDocker) -> None:
    with ComposeEnvironment(workspace, _plan(workspace), timeout_s=30, docker=docker.command):
        pass

    build = next(call for call in docker.calls() if "build" in call)
    services = build[build.index("build") + 1 :]
    assert sorted(services) == ["inventory-service", "order-service", "shared-db"]
    assert "gateway" not in services


def test_the_project_name_is_passed_to_every_command(workspace: Path, docker: FakeDocker) -> None:
    """漏传一次的表现是那条命令作用在默认项目上——并发任务于是开始互相踩。"""
    with ComposeEnvironment(workspace, _plan(workspace), timeout_s=30, docker=docker.command):
        pass

    project = compose_project("ag-1")
    assert all(project in call for call in docker.calls())


def test_teardown_happens_even_when_the_body_raises(workspace: Path, docker: FakeDocker) -> None:
    """「环境是牛不是宠物」这条全压在这一行上。"""
    with (
        pytest.raises(RuntimeError),
        ComposeEnvironment(workspace, _plan(workspace), timeout_s=30, docker=docker.command),
    ):
        raise RuntimeError("用例炸了")

    assert docker.subcommands()[-1] == "down"


def test_teardown_removes_volumes(workspace: Path, docker: FakeDocker) -> None:
    """留着卷的话下一次跑会读到上一次的数据,而「每次初始状态一致」是集成测试的前提。"""
    with ComposeEnvironment(workspace, _plan(workspace), timeout_s=30, docker=docker.command):
        pass

    down = next(call for call in docker.calls() if "down" in call)
    assert "-v" in down


def test_a_failed_start_still_tears_down(tmp_path: Path, workspace: Path) -> None:
    """健康检查没过时容器已经建出来了。不销毁的话它们会一直占着端口。"""
    docker = install_fake_docker(tmp_path / "bin", exit_codes={"up": 1})

    with (
        pytest.raises(EnvUnavailable),
        ComposeEnvironment(workspace, _plan(workspace), timeout_s=30, docker=docker.command),
    ):
        pytest.fail("环境没起来,body 不该被执行")

    assert docker.subcommands() == ["build", "up", "down"]


def test_a_failed_build_still_tears_down(tmp_path: Path, workspace: Path) -> None:
    docker = install_fake_docker(tmp_path / "bin", exit_codes={"build": 1})

    with (
        pytest.raises(EnvUnavailable),
        ComposeEnvironment(workspace, _plan(workspace), timeout_s=30, docker=docker.command),
    ):
        pytest.fail("镜像没建出来,body 不该被执行")

    assert docker.subcommands()[-1] == "down"


def test_a_hung_start_is_killed_and_torn_down(tmp_path: Path, workspace: Path) -> None:
    """卡住的环境不能永远占着资源。超时之后进程要被真的杀掉,容器要被销毁。"""
    docker = install_fake_docker(tmp_path / "bin", hang="up")

    with (
        pytest.raises(EnvTimeout),
        ComposeEnvironment(workspace, _plan(workspace), timeout_s=1, docker=docker.command),
    ):
        pytest.fail("环境卡住了,body 不该被执行")

    assert docker.subcommands()[-1] == "down"


def test_teardown_gets_a_fresh_deadline(tmp_path: Path, workspace: Path) -> None:
    """销毁不能用已经耗尽的那个额度。

    共用一个 deadline 的话,超时那一刻销毁立刻也超时,于是容器泄漏——而超时正是最需要
    销毁生效的时候。
    """
    docker = install_fake_docker(tmp_path / "bin", hang="up")

    with (
        pytest.raises(EnvTimeout),
        ComposeEnvironment(workspace, _plan(workspace), timeout_s=1, docker=docker.command),
    ):
        pytest.fail("环境卡住了,body 不该被执行")

    down = [call for call in docker.calls() if "down" in call]
    assert len(down) == 1, "销毁没跑,或者跑了不止一次"


def test_the_body_can_run_commands_against_the_environment(
    workspace: Path, docker: FakeDocker
) -> None:
    with ComposeEnvironment(
        workspace, _plan(workspace), timeout_s=30, docker=docker.command
    ) as env:
        result = env.run("order-service", ["pytest", "-q", "itest"])

    assert result.exit_code == 0
    assert docker.subcommands() == ["build", "up", "run", "down"]


def test_a_failing_command_is_reported_not_raised(workspace: Path, tmp_path: Path) -> None:
    """用例失败是集成测试的正常输出,不是异常。抛异常的话报告就写不出来了。"""
    docker = install_fake_docker(tmp_path / "bin", exit_codes={"run": 1}, stdout={"run": "挂了"})

    with ComposeEnvironment(
        workspace, _plan(workspace), timeout_s=30, docker=docker.command
    ) as env:
        result = env.run("order-service", ["pytest", "-q", "itest"])

    assert result.exit_code == 1
    assert "挂了" in result.output


def test_service_logs_are_collected(workspace: Path, tmp_path: Path) -> None:
    docker = install_fake_docker(
        tmp_path / "bin",
        stdout={"config": "order-service\ninventory-service\n", "logs": "服务启动完成\n"},
    )

    with ComposeEnvironment(
        workspace, _plan(workspace), timeout_s=30, docker=docker.command
    ) as env:
        logs = env.logs()

    assert logs["order-service"] == "服务启动完成\n"


def test_logs_cover_services_outside_the_build_closure(workspace: Path, tmp_path: Path) -> None:
    """日志要收编排文件里的**全部**服务,不只是这次构建的那几个。

    集成测试挂掉时最需要看的往往正是没被改过的那些——数据库、消息队列、网关。只收闭包的
    日志等于在最需要材料的时候没有材料。
    """
    docker = install_fake_docker(
        tmp_path / "bin",
        stdout={"config": "order-service\npostgres\n", "logs": "x\n"},
    )

    with ComposeEnvironment(
        workspace, _plan(workspace), timeout_s=30, docker=docker.command
    ) as env:
        logs = env.logs()

    assert "postgres" in logs, "没被这一轮构建的服务,日志一条都收不到"


def test_seeding_runs_before_the_cases(workspace: Path, docker: FakeDocker) -> None:
    with ComposeEnvironment(
        workspace, _plan(workspace), timeout_s=30, docker=docker.command
    ) as env:
        env.seed("order-service", ["python", "seed.py"])
        env.run("order-service", ["pytest"])

    seeded = [call for call in docker.calls() if "seed.py" in call]
    assert seeded, "灌数据的命令没跑"


def test_a_failed_seed_is_an_environment_problem(workspace: Path, tmp_path: Path) -> None:
    """没灌上就开跑的话,失败会以"业务逻辑不对"的样子出现,而实际原因是库里没数据——
    开发员工会去改一段本来没问题的代码,而且每一轮都改不好。"""
    docker = install_fake_docker(tmp_path / "bin", exit_codes={"run": 1})

    with (
        pytest.raises(EnvUnavailable),
        ComposeEnvironment(workspace, _plan(workspace), timeout_s=30, docker=docker.command) as env,
    ):
        env.seed("order-service", ["python", "seed.py"])


# --- 构建闭包 -----------------------------------------------------------------


def test_the_build_closure_covers_modules_the_plan_never_declared(tmp_path: Path) -> None:
    """闭包是"计划声明的"**并上**"diff 实际碰到的",不是二选一。

    只按计划算的话,一个计划里没写、但确实被改过的模块会用**旧镜像**参与集成测试——
    测出来的绿灯对应的不是这份代码。这正是这套集成测试存在的理由:跨模块的盲区。

    **这条性质此前只有一条 e2e 守着**,而那条 e2e 的场景(员工改了计划外的模块)在写权限
    按任务收窄之后走不通了。性质本身没变——扩权之后 diff 照样可以宽于原计划——所以把它
    挪到这条更便宜、也更直接的缝上。
    """
    from unittest.mock import patch

    from agentgenome.itest.procedure_entry import involved_modules

    project_map = {
        "version": 1,
        "project": {"name": "p"},
        "modules": [
            {"id": "order-service", "path": "repos/order-service/"},
            {"id": "inventory-service", "path": "repos/inventory-service/"},
        ],
    }
    write_flat_as_tree(tmp_path, project_map)

    with patch(
        "agentgenome.itest.procedure_entry._changed_paths",
        return_value=["repos/order-service/src/app.py"],
    ):
        found = involved_modules(tmp_path, ["inventory-service"])

    assert found == ["inventory-service", "order-service"]


def test_the_closure_falls_back_to_the_plan_when_the_diff_is_unavailable(tmp_path: Path) -> None:
    """算不出 diff 时退回计划声明的那份——比整个集成测试跑不起来好。"""
    from unittest.mock import patch

    from agentgenome.itest.procedure_entry import involved_modules

    with patch("agentgenome.itest.procedure_entry._changed_paths", return_value=[]):
        assert involved_modules(tmp_path, ["inventory-service"]) == ["inventory-service"]
