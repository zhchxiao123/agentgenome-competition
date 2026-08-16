"""knowledge-init 提示词的内容纪律(PRD 41)。

薄卡的病根之一是骨架太瘦:提示词只给"时序、坑点、不变式"三个词,员工照着写三个
bullet 就交卷。这里钉的是骨架本身——分节、减法筛、锚点写法必须在提示词里,而且
版本号要能说清某张卡是哪一版纪律下写出来的。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agentgenome.genome.knowledge import PROCEDURE_VERSION, build_prompt
from agentgenome.genome.models import Datastore, Interface, ProjectMap


def _prompt() -> str:
    project_map = ProjectMap.model_validate(
        {
            "version": 1,
            "project": {"name": "mall"},
            "modules": [{"id": "order-service", "path": "repos/order-service/"}],
        }
    )
    return build_prompt(project_map, Path("/ws"))


def test_the_card_skeleton_demands_invariants_not_feature_lists() -> None:
    prompt = _prompt()
    for section in ("承重不变量", "异常与降级路径", "测试证据", "坑点"):
        assert section in prompt, f"卡片骨架缺分节: {section}"
    # 减法筛:行为复述删掉、功能清单禁止,这两句是纪律的核心,不在提示词里等于没有。
    assert "描述行为的句子删掉" in prompt
    assert "功能清单" in prompt


def test_the_skeleton_teaches_the_anchor_forms_the_validator_checks() -> None:
    """锚点写法与 staging 校验是一对:提示词教的形状必须正是校验器认的形状。"""
    prompt = _prompt()
    assert "路径:行号" in prompt
    assert "路径::符号名" in prompt


def test_the_skeleton_sets_no_minimum_length() -> None:
    prompt = _prompt()
    assert "最少" not in prompt
    assert "至少 " not in prompt


def test_the_prompt_teaches_the_contract_index_shape_accepted_by_staging() -> None:
    """interfaces.yaml 的提示词契约必须与严格的 staging 模型一致。"""
    prompt = _prompt()

    example = prompt.split("`interfaces.yaml`(", maxsplit=1)[1].split("```yaml\n", maxsplit=1)[1]
    payload = yaml.safe_load(example.split("```", maxsplit=1)[0])
    interface_fields = {
        field.alias or name for name, field in Interface.model_fields.items()
    }
    datastore_fields = {
        field.alias or name for name, field in Datastore.model_fields.items()
    }

    assert set(payload["interfaces"][0]) == interface_fields
    assert set(payload["datastores"][0]) == datastore_fields
    Interface.model_validate(payload["interfaces"][0])
    Datastore.model_validate(payload["datastores"][0])

    for accepted_field in (
        "provider: <提供方模块id>",
        "consumers: [<消费方模块id>]",
        "schema: <Workspace 根相对的 schema 路径>",
        "owner: <归属模块id>",
        "migrations: <Workspace 根相对的迁移目录>",
        "description: 一句话说明这条契约",
        "confidence: 0.9",
    ):
        assert accepted_field in prompt, f"interfaces.yaml 缺少合法字段示例: {accepted_field}"

    assert "不要写 `name` / `path` / `summary` / `role`" in prompt


def test_version_reflects_the_discipline_change() -> None:
    """提示词契约变了,事件流里的 procedure@version 必须能区分。"""
    assert PROCEDURE_VERSION == "2.3.0"


def test_the_architecture_employee_does_not_invent_verification_commands() -> None:
    prompt = _prompt()

    assert "不要生成或修改 `test_cmd` / `build_cmd`" in prompt
    module_example = prompt.split("`modules/<模块id>/map.yaml`:", maxsplit=1)[1].split(
        "```yaml\n", maxsplit=1
    )[1]
    module_example = module_example.split("```", maxsplit=1)[0]
    assert "test_cmd" not in module_example
    assert "build_cmd" not in module_example
