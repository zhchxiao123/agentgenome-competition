"""员工的机器运行时可以从界面选,而不只是"人 / 项目缺省"两档。

写入走**既有的员工定义白名单重写**(读原文件 → 只改这一个键 → 整册严格加载 →
提交 → 事件),一行新的写入代码都不写。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from agentgenome.config import load_config
from agentgenome.employees import load_employees, workspace_employees_root
from agentgenome.server.employees_edit import (
    RuntimeNotConfigured,
    machine_runtimes,
    set_runtime,
)
from agentgenome.server.rbac import Principal, Role
from agentgenome.server.settings import Entrance
from tests.fixtures.git import commit_all, git

#: **工具要声明全**:空 allow-list 会被派发层当成只读员工(既有语义),而真实花名册
#: 里每个员工都声明了工具。不声明的 fixture 会把这一组测试推进一条现实中不存在的分支。
ARCH = """\
id: arch-employee
name: 架构员工
runtime: claude-code
prompt: prompts/arch.md
procedures: [requirement-analysis]
tools:
  allow: [Read, Grep, Glob, Write, Edit, Bash]
"""

#: 只读员工:没有 Write / Edit / Bash。**能力协商那道闸认的就是这个**。
REVIEWER = """\
id: reviewer-employee
name: 评审员工
runtime: claude-code
prompt: prompts/reviewer.md
procedures: [code-critique]
tools:
  allow: [Read, Grep, Glob]
"""

#: 会写的员工。对照组——它在哪个运行时上都跑得动。
DEV = """\
id: dev-employee
name: 开发员工
runtime: claude-code
prompt: prompts/dev.md
procedures: [code-develop]
tools:
  allow: [Read, Grep, Glob, Write, Edit, Bash]
"""

CONFIG = """\
runtime:
  default: claude-code
  claude-code: {cmd: claude}
  agentteams:
    transport: matrix-minio
    endpoint: http://controller.example.com
    consumer_token_env: AGENTTEAMS_CONSUMER_TOKEN
    matrix_homeserver: http://matrix.example.com
    matrix_token_env: AGENTTEAMS_MATRIX_TOKEN
    storage_prefix: myminio/agentteams
platform: {git_host: local}
"""


def _procedure(root: Path, procedure_id: str, runtimes: list[str]) -> None:
    """一道真的工序。**缺口是按注册表算的**——没有注册表的话它恒为空,而恒为空的
    断言测不出任何东西(这一点踩过一次)。"""
    directory = root / "genome" / "procedures" / procedure_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "procedure.yaml").write_text(
        yaml.safe_dump(
            {
                "id": procedure_id,
                "version": "1.0.0",
                "kind": "agentic",
                "prompt": "prompt.md",
                "trigger": {"states": []},
                "outputs": {"schema_ref": "schemas/out.json"},
                "compat": {"runtimes": runtimes},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (directory / "prompt.md").write_text("干活。\n", encoding="utf-8")
    (directory / "schemas").mkdir(exist_ok=True)
    (directory / "schemas" / "out.json").write_text('{"type": "object"}', encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTGENOME_GLOBAL_PROCEDURES", str(tmp_path / "global"))
    (tmp_path / "global").mkdir()
    root = tmp_path / "ws"
    prompts = root / "employees" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "arch.md").write_text("你负责项目认知。\n", encoding="utf-8")
    (prompts / "reviewer.md").write_text("你负责评审。\n", encoding="utf-8")
    (prompts / "dev.md").write_text("你负责实现。\n", encoding="utf-8")
    (root / "employees" / "arch-employee.yaml").write_text(textwrap.dedent(ARCH), encoding="utf-8")
    (root / "employees" / "reviewer-employee.yaml").write_text(
        textwrap.dedent(REVIEWER), encoding="utf-8"
    )
    (root / "employees" / "dev-employee.yaml").write_text(textwrap.dedent(DEV), encoding="utf-8")
    (root / "agentgenome.yaml").write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    _procedure(root, "requirement-analysis", ["claude-code"])
    _procedure(root, "code-develop", ["claude-code"])
    _procedure(root, "code-critique", ["claude-code"])
    git(root, "init", "-q")
    commit_all(root, "chore: 初始")
    return root


def _principal() -> Principal:
    return Principal(subject="alice", roles=frozenset({Role.ADMIN}))


def _runtime_of(root: Path, employee_id: str) -> str:
    payload = yaml.safe_load(
        (root / "employees" / f"{employee_id}.yaml").read_text(encoding="utf-8")
    )
    return str(payload["runtime"])


# --- 可选项由服务端给 -------------------------------------------------------


def test_the_options_are_the_runtimes_this_deployment_actually_configured(
    workspace: Path,
) -> None:
    """前端自己枚举的话,配置里没配的名字会出现在下拉框里——选中之后装配失败,
    而失败发生在下一次派发、不在点击那一刻。"""
    options = machine_runtimes(load_config(workspace))

    assert "claude-code" in options
    assert "agentteams" in options
    assert "human" not in options, "人不是机器运行时,它由执行档位那条路管"


def test_an_unconfigured_deployment_offers_only_what_it_has(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "agentgenome.yaml").write_text(
        "runtime:\n  default: claude-code\n  claude-code: {cmd: claude}\n", encoding="utf-8"
    )

    assert machine_runtimes(load_config(root)) == ["claude-code"]


# --- 切换 -------------------------------------------------------------------


def test_switching_to_the_container_runtime_rewrites_the_definition(workspace: Path) -> None:
    set_runtime(workspace, _principal(), "arch-employee", "agentteams", entrance=Entrance.WEB)

    assert _runtime_of(workspace, "arch-employee") == "agentteams"
    reloaded = load_employees(workspace_employees_root(workspace)).get("arch-employee")
    assert reloaded.runtime == "agentteams"


def test_the_rest_of_the_definition_survives_the_rewrite(workspace: Path) -> None:
    """白名单式重写:只改这一个键,别的字段原样留着。"""
    set_runtime(workspace, _principal(), "arch-employee", "agentteams")

    payload = yaml.safe_load(
        (workspace / "employees" / "arch-employee.yaml").read_text(encoding="utf-8")
    )
    assert payload["procedures"] == ["requirement-analysis"]
    assert payload["prompt"] == "prompts/arch.md"
    assert payload["name"] == "架构员工"


def test_switching_lands_a_commit_and_an_event(workspace: Path) -> None:
    change = set_runtime(workspace, _principal(), "arch-employee", "agentteams")

    assert change.actor == "alice"
    assert "arch-employee" in change.section


def test_an_unconfigured_runtime_is_refused_before_anything_is_written(
    workspace: Path,
) -> None:
    """配置里没有的运行时选中之后装配不出来——那种失败要等到下一次派发才出现。"""
    with pytest.raises(RuntimeNotConfigured, match="qwen-code"):
        set_runtime(workspace, _principal(), "arch-employee", "qwen-code")

    assert _runtime_of(workspace, "arch-employee") == "claude-code", "拒绝时不该已经写了"


def test_switching_to_human_is_refused_here(workspace: Path) -> None:
    """人不是机器运行时:转 manual 要走执行档位那条路,它还管指派人与确认名单。"""
    with pytest.raises(RuntimeNotConfigured, match="human|执行档位"):
        set_runtime(workspace, _principal(), "arch-employee", "human")


def test_switching_to_the_same_runtime_writes_nothing(workspace: Path) -> None:
    """重复点一次同一个选项是常事。**不写空提交**,但也不报错。"""
    before = git(workspace, "rev-parse", "HEAD")

    set_runtime(workspace, _principal(), "arch-employee", "claude-code")

    assert git(workspace, "rev-parse", "HEAD") == before


# --- REST -------------------------------------------------------------------


def _client(root: Path):
    from fastapi.testclient import TestClient

    from agentgenome.server.app import create_app

    return TestClient(create_app(root))


def test_the_runtime_options_come_from_the_server(workspace: Path) -> None:
    response = _client(workspace).get("/employees/arch-employee/runtime")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["current"] == "claude-code"
    assert set(payload["options"]) == {"claude-code", "agentteams"}


def test_the_endpoint_lists_the_compat_gap_for_a_candidate_runtime(workspace: Path) -> None:
    """切之前就看到差距,而不是派发时才撞上兼容闸。

    `arch-employee` 的 `requirement-analysis` 只声明了 claude-code,所以换到 agentteams
    之前它就是那道差距。
    """
    response = _client(workspace).get(
        "/employees/arch-employee/runtime", params={"candidate": "agentteams"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["compat_gap"] == ["requirement-analysis"]


def test_no_candidate_means_no_gap_is_computed(workspace: Path) -> None:
    """**否定断言**:没给候选就只是展示现状,不去算任何差距。"""
    body = _client(workspace).get("/employees/arch-employee/runtime").json()

    assert body["compat_gap"] == []


def test_switching_through_the_api_rewrites_the_definition(workspace: Path) -> None:
    response = _client(workspace).put(
        "/employees/arch-employee/runtime", json={"runtime": "agentteams"}
    )

    assert response.status_code == 200, response.text
    assert _runtime_of(workspace, "arch-employee") == "agentteams"


def test_an_unconfigured_runtime_is_a_422_not_a_500(workspace: Path) -> None:
    response = _client(workspace).put(
        "/employees/arch-employee/runtime", json={"runtime": "qwen-code"}
    )

    assert response.status_code == 422
    assert "qwen-code" in response.text


def test_declaring_compat_through_the_api(workspace: Path) -> None:
    response = _client(workspace).post(
        "/employees/arch-employee/runtime/compat",
        json={"runtime": "agentteams", "procedures": []},
    )

    assert response.status_code == 200, response.text


# --- 能力协商:兼容闸之外的另一道 ---------------------------------------------


def test_a_read_only_employee_is_told_a_runtime_cannot_take_it(workspace: Path) -> None:
    """**兼容缺口不是唯一会挡下派发的东西。**

    `reviewer-employee` 只读(没有 Write/Edit/Bash),而 agentteams 强制不了只读——
    `dispatch._check_runtime_guarantees` 会拒。补完兼容声明也一样拒,而那句报错说的是
    别的事。不在点击那一刻讲清楚的话,人会先把一个运行时写进版本化资产,再对着一句
    完全不相干的错误发愣。
    """
    client = _client(workspace)

    body = client.get("/employees/reviewer-employee/runtime").json()

    blocked = {row["runtime"]: row["reason"] for row in body["blocked"]}
    assert "agentteams" in blocked
    assert "只读" in blocked["agentteams"]


def test_a_runtime_that_can_take_the_employee_is_not_listed_as_blocked(workspace: Path) -> None:
    """**否定断言**:能跑的不该出现在拦截清单里,否则那份清单没人会再看。"""
    client = _client(workspace)

    body = client.get("/employees/dev-employee/runtime").json()

    assert [row["runtime"] for row in body["blocked"]] == []


def test_a_blocked_runtime_does_not_also_advertise_a_compat_gap(workspace: Path) -> None:
    """**两条互相拆台的提示只能留一条。**

    `reviewer-employee` 换不到 agentteams(只读),补多少声明都没用。这时还列出缺口、
    还给一个补声明按钮,等于告诉他"照这几步做就能跑"——而那是假的,并且照做会把一个
    它永远跑不到的运行时写进版本化资产。
    """
    client = _client(workspace)

    body = client.get("/employees/reviewer-employee/runtime?candidate=agentteams").json()

    assert [row["runtime"] for row in body["blocked"]] == ["agentteams"]
    assert body["compat_gap"] == [], "接不了的运行时不该再列缺口"


def test_a_gap_is_still_reported_for_a_runtime_that_could_take_the_employee(
    workspace: Path,
) -> None:
    """**否定断言**:别把缺口一并压掉——能跑的那些,缺口正是他要看的东西。"""
    client = _client(workspace)

    body = client.get("/employees/dev-employee/runtime?candidate=agentteams").json()

    assert body["blocked"] == []
    assert body["compat_gap"] == ["code-develop"]


def test_declaring_compat_for_a_blocked_runtime_is_refused(workspace: Path) -> None:
    """**藏起按钮不是边界**——一条手滑的 curl 一样能把假声明写进 git。"""
    client = _client(workspace)

    response = client.post(
        "/employees/reviewer-employee/runtime/compat",
        json={"runtime": "agentteams", "procedures": ["code-critique"]},
    )

    assert response.status_code == 422, response.text
    assert "只读" in response.json()["detail"]
