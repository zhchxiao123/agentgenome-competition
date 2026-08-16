"""staging 形态的知识产出夹具(PRD 34)。

契约改造之后,架构员工的产出是产物目录 `staging/` 下的真实树文件,不再是 JSON 信封。
录制因此携带 `outputs/staging/**`(回放写进产物目录)+ 一张小票 `result.json`。

这里的构造器把"一个模块的认知"翻成那组文件——键是 staging 相对路径,值是文件内容。
测试想构造坏产出时,直接覆盖某个键就是"坏了哪个文件",与真实失败形态一一对应。
"""

from __future__ import annotations

import json
from typing import Any

import yaml

#: 默认小票。**没有 passed 字段**——自述不参与裁决,成败由 staging 校验说了算。
RECEIPT = {"task_id": "knowledge-init", "producer": "arch-employee"}


def build_staging(
    modules: list[dict[str, Any]],
    interfaces: list[dict[str, Any]] | None = None,
    datastores: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """把若干模块认知翻成 staging 文件集(staging 相对路径 → 内容)。

    每个模块条目的形状:

    - ``id`` / ``path``:与项目地图一致;
    - ``summary``:进根索引补丁;
    - ``map``:模块地图的认知字段(lang / test_cmd / confidence …);
    - ``overview``:模块认知卡正文,``None`` 表示这次不写;
    - ``features``:功能点清单,每条带 ``card_text``(卡片文件内容)或 ``no_card``。
    """
    files: dict[str, str] = {}
    patch_entries = []
    for module in modules:
        module_id = str(module["id"])
        patch_entries.append(
            {
                "id": module_id,
                "path": module.get("path", f"repos/{module_id}/"),
                "summary": module.get("summary", f"{module_id} 的认知"),
                "map": f"modules/{module_id}/map.yaml",
            }
        )
        payload: dict[str, Any] = {
            "id": module_id,
            "lang": "python",
            "test_cmd": "pytest -q",
            "confidence": 0.9,
            **(module.get("map") or {}),
        }
        # 显式给 None 表示"这份产出不带这个字段"——测试用它构造缺字段的坏产出。
        payload = {key: value for key, value in payload.items() if value is not None}
        overview = module.get("overview", f"# {module_id}\n\n这个模块是干什么的。\n")
        if overview is not None:
            payload["doc"] = f"genome/knowledge/modules/{module_id}/overview.md"
            files[f"modules/{module_id}/overview.md"] = overview
        if "features" in module:
            entries = []
            for feature in module["features"] or []:
                entry = {
                    key: value
                    for key, value in feature.items()
                    if key not in ("card_text",)
                }
                if "card_text" in feature:
                    entry.setdefault("card", f"features/{feature['id']}.md")
                    files[f"modules/{module_id}/features/{feature['id']}.md"] = str(
                        feature["card_text"]
                    )
                entries.append(entry)
            payload["features"] = entries
        files[f"modules/{module_id}/map.yaml"] = yaml.safe_dump(
            payload, allow_unicode=True, sort_keys=False
        )
    files["project-map.yaml"] = yaml.safe_dump(
        {"modules": patch_entries}, allow_unicode=True, sort_keys=False
    )
    if interfaces is not None or datastores is not None:
        files["interfaces.yaml"] = yaml.safe_dump(
            {"interfaces": interfaces or [], "datastores": datastores or []},
            allow_unicode=True,
            sort_keys=False,
        )
    return files


def card(feature_id: str, body: str = "时序与坑点。") -> str:
    """一张最小的合法功能卡片。"""
    return f"---\nid: {feature_id}\nconfidence: high\n---\n\n{body}\n"


def receipt_json(**extra: Any) -> str:
    return json.dumps({**RECEIPT, **extra}, ensure_ascii=False)


__all__ = ["RECEIPT", "build_staging", "card", "receipt_json"]
