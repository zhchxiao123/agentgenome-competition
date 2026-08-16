"""员工供应:soul 组装、所有权命名,以及经桩服务器驱动的真实 HTTP 实现。

soul 与命名是纯函数,直接断言、不需要替身;HTTP 实现走本地桩服务器,
与 matrix-minio 传输的测法同源,不起任何真实平台。
"""

from __future__ import annotations

from typing import Any

import pytest

from agentgenome.agents.agentteams.provision import (
    HttpWorkerProvisioner,
    ModelTier,
    ProvisionError,
    UnknownModelTier,
    assemble_soul,
    build_payload,
    is_ours,
    worker_name,
)
from agentgenome.agents.agentteams.transport import PlatformUnavailable
from agentgenome.employees import EmployeeConfig
from tests.fixtures.fake_worker_platform import FakeWorkerPlatform

ROLE_PROMPT = """\
# 架构数字员工

你负责**项目认知**:读代码,把项目地图、模块卡片、接口契约与决策记录补全并保持准确。
你不写业务代码——那是开发员工的活。

你的判断被后续所有任务读到。因此宁可写"这里不确定",也不要写一个看起来笃定的猜测。

每条认知带上 `confidence`。读代码读出来的填高,从命名和目录结构推出来的填低。

## 铁律

绝不伪造测试通过。绝不把没跑过的命令写成跑过了。
"""


def _employee(**overrides: Any) -> EmployeeConfig:
    fields: dict[str, Any] = {
        "id": "arch-employee",
        "name": "架构员工",
        "runtime": "agentteams",
        "prompt": "prompts/arch.md",
        "prompt_text": ROLE_PROMPT,
    }
    fields.update(overrides)
    return EmployeeConfig(**fields)


# --- soul 组装 ---------------------------------------------------------------


def test_soul_declares_who_governs_and_where_the_contract_lives() -> None:
    """Worker 侧运行时每轮都读自己的身份文件——身份必须在那里立住,
    否则平台预设人格(比如"你是 QA 工程师")会被当成本职。"""
    soul = assemble_soul(_employee())

    assert "AgentGenome" in soul
    assert "spec.md" in soul, "要说清每次以什么为准"
    assert "artifacts/result.json" in soul, "要说清产物写哪"
    assert "所有产物" in soul
    assert "最后" in soul, "终态必须是提交屏障,不能先报成功再慢慢传文件"
    assert "SUCCESS" in soul, "要说清完成后置什么状态"


def test_soul_carries_a_bounded_role_summary_not_the_whole_prompt() -> None:
    """人格由每个 Job 的上下文包注入。soul 里放全份提示词会陈旧,
    而陈旧的人格与新鲜的上下文同时在场时,冲突是静默的。"""
    soul = assemble_soul(_employee())

    assert "架构员工" in soul, "要认得出自己是谁"
    assert "项目认知" in soul, "概要要有实质,不能只剩一个名字"
    assert "绝不伪造测试通过" not in soul, "提示词深处的内容不该进 soul"
    assert len(soul) < len(ROLE_PROMPT) + 800, "soul 要有界,不能把整份提示词抄进去"


def test_soul_is_stable_for_an_unchanged_employee() -> None:
    """幂等对齐靠它:同一份定义两次组装必须逐字节相同。"""
    assert assemble_soul(_employee()) == assemble_soul(_employee())


def test_an_employee_without_a_prompt_still_gets_an_identity() -> None:
    soul = assemble_soul(_employee(prompt_text=""))

    assert "AgentGenome" in soul
    assert "spec.md" in soul


# --- 所有权命名 -------------------------------------------------------------


def test_worker_name_marks_ownership() -> None:
    name = worker_name("arch-employee")

    assert is_ours(name)
    assert "arch-employee" in name, "名字要能让人一眼看出是哪个员工"


def test_foreign_workers_are_not_ours() -> None:
    """平台上人工建的 Worker 必须原样存活——判据不能靠猜。"""
    for foreign in ("developer", "qa-tester", "requirements-analyst", "arch-employee"):
        assert not is_ours(foreign), f"{foreign} 不是我们供应的"

# --- HTTP 实现(经"真实的假平台")----------------------------------------------


def _provisioner(platform: FakeWorkerPlatform, **overrides: Any) -> HttpWorkerProvisioner:
    fields: dict[str, Any] = {
        "endpoint": platform.url,
        "token": "sk-consumer-1",
        "poll_interval_s": 0.01,
        "ready_timeout_s": 1.0,
    }
    fields.update(overrides)
    return HttpWorkerProvisioner(**fields)


NAME = worker_name("arch-employee")


async def test_an_absent_worker_is_created_and_its_room_returned() -> None:
    with FakeWorkerPlatform() as platform:
        outcome = await _provisioner(platform).reconcile(_employee())

    assert outcome.action == "created"
    assert outcome.ref.name == NAME
    assert outcome.ref.room_id == f"!room-{NAME}:example.com"
    assert "AgentGenome" in platform.workers[NAME]["soul"]


async def test_an_unchanged_worker_is_left_alone() -> None:
    """幂等:没有变化时零平台写动作,命令才敢放进部署脚本反复跑。"""
    with FakeWorkerPlatform() as platform:
        provisioner = _provisioner(platform)
        await provisioner.reconcile(_employee())
        before = len(platform.records)

        outcome = await provisioner.reconcile(_employee())
        writes = [
            r for r in platform.records[before:] if r["method"] in ("POST", "PUT", "DELETE")
        ]

    assert outcome.action == "unchanged"
    assert writes == [], "没变化就不该有任何写动作"


async def test_a_drifted_worker_is_updated_not_recreated() -> None:
    with FakeWorkerPlatform() as platform:
        provisioner = _provisioner(platform)
        await provisioner.reconcile(_employee())
        before = len(platform.records)

        changed = _employee(prompt_text="# 新角色\n\n你现在负责别的事情了。\n")
        outcome = await provisioner.reconcile(changed)
        methods = [r["method"] for r in platform.records[before:] if r["method"] != "GET"]

    assert outcome.action == "updated"
    assert methods == ["PUT"], "有变化要更新,不是重建——重建会换掉房间"
    assert "别的事情" in platform.workers[NAME]["soul"]


async def test_a_foreign_worker_with_the_same_employee_name_is_untouched() -> None:
    """平台上人工建的 Worker 必须原样存活。"""
    with FakeWorkerPlatform() as platform:
        platform.preset("arch-employee", soul="# 我是别人建的\n")

        await _provisioner(platform).reconcile(_employee())
        touched = [
            r["path"] for r in platform.records if r["method"] in ("PUT", "DELETE")
        ]

    assert platform.workers["arch-employee"]["soul"] == "# 我是别人建的\n"
    assert all(NAME in path for path in touched)


async def test_the_consumer_token_travels_in_the_header_not_the_url() -> None:
    """URL 会进各级访问日志。"""
    with FakeWorkerPlatform() as platform:
        await _provisioner(platform).reconcile(_employee())
        post = [r for r in platform.records if r["method"] == "POST"][0]

    assert post["headers"].get("Authorization", "").startswith("Bearer ")
    assert all("sk-consumer-1" not in r["path"] for r in platform.records)


async def test_resolve_returns_none_for_an_employee_without_a_worker() -> None:
    with FakeWorkerPlatform() as platform:
        assert await _provisioner(platform).resolve("arch-employee") is None


async def test_resolve_returns_the_current_room() -> None:
    with FakeWorkerPlatform() as platform:
        provisioner = _provisioner(platform)
        await provisioner.reconcile(_employee())

        ref = await provisioner.resolve("arch-employee")

    assert ref is not None
    assert ref.room_id == f"!room-{NAME}:example.com"


async def test_a_worker_that_never_becomes_ready_times_out() -> None:
    """平台受理了但没起来——如实报,不无限等。"""
    with (
        FakeWorkerPlatform(never_ready=True) as platform,
        pytest.raises(ProvisionError, match="就绪"),
    ):
        await _provisioner(platform).reconcile(_employee())


async def test_a_worker_without_a_room_is_not_considered_ready() -> None:
    """没有房间就派不了活,报 Running 也没用。"""
    with (
        FakeWorkerPlatform(no_room=True) as platform,
        pytest.raises(ProvisionError, match="就绪|房间"),
    ):
        await _provisioner(platform).reconcile(_employee())


async def test_platform_failure_is_attributed_to_the_platform() -> None:
    with (
        FakeWorkerPlatform(fail_status=500) as platform,
        pytest.raises(PlatformUnavailable),
    ):
        await _provisioner(platform).reconcile(_employee())


# --- 模型档位(issue 04)------------------------------------------------------


def test_an_unmapped_deployment_sends_no_model_fields() -> None:
    """没配映射的部署行为零变化:平台用它自己的默认。"""
    payload = build_payload(_employee(), tiers={})

    assert "model" not in payload
    assert "modelProvider" not in payload


def test_a_mapped_tier_becomes_platform_model_fields() -> None:
    tiers = {"cheap": ModelTier(model="qwen-turbo", provider="dashscope")}

    payload = build_payload(_employee(model="cheap"), tiers=tiers)

    assert payload["model"] == "qwen-turbo"
    assert payload["modelProvider"] == "dashscope"


def test_two_tiers_yield_different_payloads() -> None:
    """深读走便宜档、判断走强档——这是这条接线的全部意义。"""
    tiers = {
        "cheap": ModelTier(model="qwen-turbo"),
        "strong": ModelTier(model="qwen-max"),
    }

    cheap = build_payload(_employee(model="cheap"), tiers=tiers)
    strong = build_payload(_employee(model="strong"), tiers=tiers)

    assert cheap["model"] != strong["model"]


def test_a_tier_without_a_provider_omits_the_provider_field() -> None:
    """提供商是可选的:单提供商部署不该被逼着填一个占位值。"""
    payload = build_payload(_employee(model="cheap"), tiers={"cheap": ModelTier(model="q")})

    assert payload["model"] == "q"
    assert "modelProvider" not in payload


def test_an_unknown_tier_is_refused_and_names_the_employee() -> None:
    """拼错的档位不该静默退回默认档——那会让一个本该便宜的批量作业悄悄走贵的。"""
    tiers = {"cheap": ModelTier(model="qwen-turbo")}

    with pytest.raises(UnknownModelTier) as excinfo:
        build_payload(_employee(model="chaep"), tiers=tiers)

    assert "arch-employee" in str(excinfo.value)
    assert "chaep" in str(excinfo.value)
    assert "cheap" in str(excinfo.value), "要告诉使用者有哪些档位可选"


def test_the_default_tier_is_not_special_cased() -> None:
    """`default` 只是一个档位名。配了就用,没配就不下发——没有隐藏的兜底模型。"""
    assert "model" not in build_payload(_employee(model="default"), tiers={})
    payload = build_payload(_employee(model="default"), tiers={"default": ModelTier(model="m")})
    assert payload["model"] == "m"


# --- 生命周期(issue 05)------------------------------------------------------


async def test_sleeping_a_worker_stops_its_container() -> None:
    with FakeWorkerPlatform() as platform:
        provisioner = _provisioner(platform)
        await provisioner.reconcile(_employee())

        await provisioner.sleep("arch-employee")

    assert platform.workers[NAME]["state"] == "Sleeping"


async def test_deleting_removes_only_our_own_worker() -> None:
    with FakeWorkerPlatform() as platform:
        platform.preset("arch-employee", soul="# 别人建的\n")
        provisioner = _provisioner(platform)
        await provisioner.reconcile(_employee())

        await provisioner.delete("arch-employee")

    assert NAME not in platform.workers
    assert "arch-employee" in platform.workers, "外人的 Worker 必须原样存活"


async def test_waking_a_sleeping_worker_brings_it_back() -> None:
    with FakeWorkerPlatform() as platform:
        provisioner = _provisioner(platform)
        await provisioner.reconcile(_employee())
        await provisioner.sleep("arch-employee")

        ref = await provisioner.wake("arch-employee")

    assert ref.sleeping is False
    assert platform.workers[NAME]["state"] == "Running"


async def test_waking_an_already_running_worker_does_not_disturb_it() -> None:
    """已经在跑的不该被多余的唤醒调用打扰。"""
    with FakeWorkerPlatform() as platform:
        provisioner = _provisioner(platform)
        await provisioner.reconcile(_employee())
        before = len(platform.records)

        await provisioner.wake("arch-employee")
        wakes = [r for r in platform.records[before:] if r["path"].endswith("/wake")]

    assert wakes == []


async def test_a_worker_lost_before_wake_points_at_automatic_retry() -> None:
    with FakeWorkerPlatform() as platform, pytest.raises(ProvisionError, match="自动重建"):
        await _provisioner(platform).wake("arch-employee")


# --- 评审修正 ---------------------------------------------------------------


async def test_a_tier_change_is_detected_as_drift() -> None:
    """幂等比较不能只看 soul:换了档位却报"无变化",员工会一直跑在旧模型上,
    而账单要到月底才说话。"""
    with FakeWorkerPlatform() as platform:
        cheap = {"cheap": ModelTier(model="qwen-turbo"), "strong": ModelTier(model="qwen-max")}
        provisioner = _provisioner(platform, tiers=cheap)
        await provisioner.reconcile(_employee(model="cheap"))

        outcome = await provisioner.reconcile(_employee(model="strong"))

    assert outcome.action == "updated"
    assert platform.workers[NAME]["model"] == "qwen-max"


async def test_an_employee_id_with_a_path_separator_cannot_escape_its_worker() -> None:
    """名字直接拼进 URL 的话,带分隔符的 id 会指到另一个资源上。"""
    with FakeWorkerPlatform() as platform:
        provisioner = _provisioner(platform)

        with pytest.raises(ProvisionError, match="不属于我们|非法"):
            await provisioner.delete("../developer")

    assert platform.records == [], "非法目标不该产生任何平台请求"
