"""全局基因库（PRD 17）与多工作区 / 角色权限（PRD 18）。

两者共享一条原则:**默认拒绝**。基因库的默认是"不许上交"(污染半径是所有项目),多工作区的
默认是"不许落到某个工作区"(默认工作区是跨租户泄漏最常见的来源)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentgenome.genome.evolution.cards import Applicability, Evidence, LessonCard
from agentgenome.registry.library import Registry, RegistryUnavailable, check_contribution
from agentgenome.server.rbac import ANONYMOUS, Action, Forbidden, Principal, Role, approvers_of
from agentgenome.server.tenancy import (
    AmbiguousWorkspace,
    UnknownWorkspace,
    WorkspaceRegistry,
)


def _card(**overrides) -> LessonCard:
    base = LessonCard(
        id="L-0001",
        title="超时要带上重试上限",
        applies_to=Applicability(scenario="调用外部 HTTP 接口"),
        conclusion="设了超时不设重试上限,失败时会把下游打垮。",
        evidence=[Evidence(task_id="ag-1", path="artifacts/x.json")],
        hits=5,
    )
    return base.model_copy(update=overrides)


def _registry(tmp_path: Path) -> Registry:
    root = tmp_path / "lib"
    tpl = root / "templates" / "python-service" / "genome" / "rules"
    tpl.mkdir(parents=True)
    (root / "templates" / "python-service" / "template.yaml").write_text(
        "summary: Python 服务骨架\nshape: python-service\n", encoding="utf-8"
    )
    (tpl / "coding.md").write_text("# 编码规范\n", encoding="utf-8")
    procedure = root / "procedures" / "changelog-write"
    procedure.mkdir(parents=True)
    (procedure / "procedure.yaml").write_text("id: changelog-write\n", encoding="utf-8")
    return Registry(root=root, name="global")


# --- 基因库 -----------------------------------------------------------------


def test_a_missing_registry_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(RegistryUnavailable) as error:
        Registry(root=tmp_path / "nope").ensure()

    assert "Git 仓库" in str(error.value)


def test_templates_can_be_listed(tmp_path: Path) -> None:
    found = _registry(tmp_path).templates()

    assert [item.name for item in found] == ["python-service"]
    assert found[0].shape == "python-service"


def test_applying_a_template_records_where_it_came_from(tmp_path: Path) -> None:
    """三个月后有人问「这条规则为什么长这样」,唯一能顺藤摸瓜的入口就是来源。"""
    ws = tmp_path / "ws"
    _registry(tmp_path).apply_template("python-service", ws)

    body = (ws / "genome" / "rules" / "coding.md").read_text(encoding="utf-8")
    assert "全局基因库" in body
    assert "起点不是终点" in body


def test_applying_a_template_never_overwrites(tmp_path: Path) -> None:
    """**模板是起跑线,不是重置按钮。** 一次误用不该抹掉项目已经积累的认知。"""
    ws = tmp_path / "ws"
    target = ws / "genome" / "rules"
    target.mkdir(parents=True)
    (target / "coding.md").write_text("项目自己写的规范\n", encoding="utf-8")

    written = _registry(tmp_path).apply_template("python-service", ws)

    assert written == []
    assert (target / "coding.md").read_text(encoding="utf-8") == "项目自己写的规范\n"


def test_pulling_a_procedure_that_already_exists_is_refused(tmp_path: Path) -> None:
    """静默覆盖会把项目自己的改动抹掉。"""
    ws = tmp_path / "ws"
    registry = _registry(tmp_path)
    registry.pull_procedure("changelog-write", ws)

    with pytest.raises(FileExistsError):
        registry.pull_procedure("changelog-write", ws)


def test_an_unknown_template_lists_what_exists(tmp_path: Path) -> None:
    with pytest.raises(KeyError) as error:
        _registry(tmp_path).template("没这个")

    assert "python-service" in str(error.value)


# --- 上交的审查 -------------------------------------------------------------


def test_a_lesson_that_never_helped_cannot_be_contributed() -> None:
    """**一条从没帮上过忙的经验,没有理由推给所有人。**"""
    verdict = check_contribution(_card(hits=0))

    assert not verdict.allowed
    assert "命中 0 次" in verdict.reasons[0]


def test_force_can_override_the_hit_threshold() -> None:
    assert check_contribution(_card(hits=0), force=True).allowed


def test_a_project_specific_name_is_a_warning_not_a_rejection() -> None:
    """判断「是不是真的通用」最终要靠人,而误拒会让真正有价值的经验上不来。"""
    verdict = check_contribution(_card(conclusion="order-service 的预占接口要幂等"))

    assert verdict.allowed
    assert any("项目特有" in item for item in verdict.warnings)


def test_naming_modules_warns_because_module_ids_do_not_exist_elsewhere() -> None:
    verdict = check_contribution(_card(applies_to=Applicability(modules=["order-service"])))

    assert any("模块 id" in item for item in verdict.warnings)


def test_a_concrete_mount_path_is_project_specific_by_construction() -> None:
    """挂载路径带着本项目的仓库名,**按构造**就不通用。

    这是语义化挂载点之后最强的一个信号:此前挂载点是位置编号,泄漏进来看着人畜无害;
    现在它直接印着 `order-service`,而全局库的判据是"与具体项目无关"。
    """
    verdict = check_contribution(
        _card(conclusion="改 repos/order-service/src/reserve.py 之前先看迁移目录")
    )

    assert verdict.allowed
    assert any("项目特有" in item for item in verdict.warnings)
    assert any("repos/order-service" in item for item in verdict.warnings)


def test_the_mount_root_alone_is_not_a_leak() -> None:
    """`repos/**` 是这套约定本身,每个项目都长这样——它不指向任何一个具体项目。

    抓它只会让"权限范围是 repos/** 时要注意…"这类真·通用经验被误标。
    """
    verdict = check_contribution(_card(conclusion="员工的可写范围是 repos/** 时要留意越权复查"))

    assert verdict.warnings == ()


def test_a_genuinely_generic_lesson_passes_clean() -> None:
    verdict = check_contribution(_card())

    assert verdict.allowed
    assert verdict.warnings == ()


# --- 多工作区 ---------------------------------------------------------------


def test_a_single_workspace_needs_no_name(tmp_path: Path) -> None:
    """单工作区部署仍然要能跑。**那不是默认值,是唯一解。**"""
    registry = WorkspaceRegistry()
    registry.register("mall", tmp_path)

    assert registry.resolve(None)[0] == "mall"


def test_two_workspaces_make_an_unqualified_request_an_error(tmp_path: Path) -> None:
    """**默认工作区是跨租户数据泄漏最常见的来源。**

    调用方忘了带参数,于是它读到了别人的任务——而这件事没有任何症状。
    """
    registry = WorkspaceRegistry()
    registry.register("mall", tmp_path)
    registry.register("shop", tmp_path)

    with pytest.raises(AmbiguousWorkspace):
        registry.resolve(None)


def test_an_unknown_workspace_lists_what_exists(tmp_path: Path) -> None:
    registry = WorkspaceRegistry()
    registry.register("mall", tmp_path)

    with pytest.raises(UnknownWorkspace) as error:
        registry.resolve("nope")

    assert "mall" in str(error.value)


@pytest.mark.parametrize("name", ["../etc", "a/b", "", "x" * 65, ".hidden"])
def test_a_name_that_could_traverse_paths_is_refused(name: str, tmp_path: Path) -> None:
    """名字不是路径。让调用方直接传路径的话,`?workspace=/etc` 就是一次任意目录读取。"""
    with pytest.raises(ValueError):
        WorkspaceRegistry().register(name, tmp_path)


# --- 角色权限 ---------------------------------------------------------------


def test_an_unauthenticated_principal_can_do_nothing() -> None:
    """默认拒绝。"""
    assert not any(ANONYMOUS.allows(action) for action in Action)


def test_an_approver_cannot_submit_requirements() -> None:
    """**审自己写的东西不叫审批。**"""
    approver = Principal("alice", frozenset({Role.APPROVER}))

    assert approver.allows(Action.APPROVE_TASK)
    assert not approver.allows(Action.SUBMIT_TASK)


def test_a_developer_cannot_approve() -> None:
    developer = Principal("bob", frozenset({Role.DEVELOPER}))

    with pytest.raises(Forbidden) as error:
        developer.ensure(Action.APPROVE_TASK)

    assert "前端藏起按钮不是安全边界" in str(error.value)


def test_an_admin_can_do_everything() -> None:
    admin = Principal("root", frozenset({Role.ADMIN}))

    assert all(admin.allows(action) for action in Action)


def test_multiple_roles_are_a_union() -> None:
    both = Principal("carol", frozenset({Role.DEVELOPER, Role.APPROVER}))

    assert both.allows(Action.SUBMIT_TASK)
    assert both.allows(Action.APPROVE_TASK)


def test_the_approver_list_comes_from_the_matrix() -> None:
    """PRD 08 的名单换成它之后,校验位置一行不用改。"""
    people = [
        Principal("alice", frozenset({Role.APPROVER})),
        Principal("bob", frozenset({Role.DEVELOPER})),
        Principal("root", frozenset({Role.ADMIN})),
    ]

    assert approvers_of(people) == ["alice", "root"]


# --- 系统设置与审计 ---------------------------------------------------------


def test_a_settings_change_records_who_did_it(tmp_path: Path) -> None:
    """一个没有操作人的配置变更，在出事时无法回答「这个并发数是谁调上去的」。

    **前值后值不再进记录**——它们去版本面查，理由见 `server.settings`。这条以前断言
    `before`/`after`，改记录平面时一并改掉了：留着的话它会把一个已经被否掉的设计钉死。
    """
    from agentgenome.server.settings import history, update

    (tmp_path / "agentgenome.yaml").write_text("concurrency:\n  global_jobs: 3\n", encoding="utf-8")
    admin = Principal("root", frozenset({Role.ADMIN}))

    update(tmp_path, admin, "concurrency", {"global_jobs": 8})

    records = history(tmp_path)
    assert records[-1].actor == "root"
    assert records[-1].section == "concurrency"
    assert records[-1].before is None
    assert records[-1].after is None


def test_the_old_settings_audit_log_still_reads_back(tmp_path: Path) -> None:
    """旧审计日志不迁移也不丢。

    迁移要重写它们的时间语义与身份来源，那是拿「当时到底怎么记的」去换整齐——审计日志恰恰
    不该被后来的人修饰。
    """
    from agentgenome.server.settings import SETTINGS_AUDIT, history

    legacy = tmp_path / SETTINGS_AUDIT
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps(
            {
                "actor": "old-admin",
                "section": "budgets",
                "before": {"per_task_tokens": 1},
                "after": {"per_task_tokens": 2},
                "at": "2020-01-01T00:00:00+00:00",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    records = history(tmp_path)

    assert [item.actor for item in records] == ["old-admin"]
    assert records[0].before == {"per_task_tokens": 1}


def test_a_non_admin_cannot_change_settings(tmp_path: Path) -> None:
    """**权限校验在服务端。** 前端藏起表单只是展示裁剪。"""
    from agentgenome.server.settings import update

    (tmp_path / "agentgenome.yaml").write_text("{}\n", encoding="utf-8")

    with pytest.raises(Forbidden):
        update(tmp_path, Principal("bob", frozenset({Role.DEVELOPER})), "budgets", {})


def test_sections_outside_the_settings_whitelist_cannot_be_edited_from_the_ui(
    tmp_path: Path,
) -> None:
    """放开运行额度不等于把平台与员工定义也变成任意写入口。"""
    from agentgenome.server.settings import NotEditable, update

    (tmp_path / "agentgenome.yaml").write_text("{}\n", encoding="utf-8")

    with pytest.raises(NotEditable):
        update(tmp_path, Principal("root", frozenset({Role.ADMIN})), "platform", {})


def test_the_config_file_stays_the_source_of_truth(tmp_path: Path) -> None:
    """做成数据库里的一份副本的话,会出现「文件里写着 3、界面上显示 5」这种谁都说不清的状态。"""
    import yaml as yaml_module

    from agentgenome.server.settings import update

    (tmp_path / "agentgenome.yaml").write_text("concurrency:\n  global_jobs: 3\n", encoding="utf-8")
    update(tmp_path, Principal("root", frozenset({Role.ADMIN})), "concurrency", {"global_jobs": 8})

    payload = yaml_module.safe_load((tmp_path / "agentgenome.yaml").read_text(encoding="utf-8"))
    assert payload["concurrency"]["global_jobs"] == 8
