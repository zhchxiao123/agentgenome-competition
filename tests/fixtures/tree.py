"""把一份扁平的项目地图文本摆成知识树。

地图在磁盘上分了三层之后,测试如果各自手摆三个文件,"一份地图长什么样"就会有十几个略有
出入的版本,而且每加一层就要全部改一遍。

**这里刻意不做校验。** 校验用例要摆的正是非法的树——未知字段、缺必填项。按键的归属把它们
分发到对应的文件里,让真正的加载器去报错,测的才是加载器而不是这个夹具。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agentgenome import paths
from agentgenome.genome.tree import MODULE_MAP

#: 模块条目里属于根索引的键。其余的一律沉到模块地图——包括拼错的那些,
#: 这样"未知字段"报在它本该待的那个文件上。
_ROOT_MODULE_KEYS = {"id", "path", "summary"}


def write_flat_as_tree(root: Path, flat: dict[str, Any]) -> Path:
    """按知识树的分工摆开一份扁平地图。"""
    knowledge = root / paths.KNOWLEDGE
    knowledge.mkdir(parents=True, exist_ok=True)

    entries = []
    for module in flat.get("modules") or []:
        if not isinstance(module, dict):
            entries.append(module)
            continue
        entry = {key: value for key, value in module.items() if key in _ROOT_MODULE_KEYS}
        rest = {key: value for key, value in module.items() if key not in _ROOT_MODULE_KEYS}
        module_id = str(module.get("id") or "unnamed")
        entry["map"] = str(Path("modules") / module_id / MODULE_MAP)
        rest.setdefault("id", module_id)
        target = knowledge / "modules" / module_id
        target.mkdir(parents=True, exist_ok=True)
        (target / MODULE_MAP).write_text(
            yaml.safe_dump(rest, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        entries.append(entry)

    contracts = {
        "interfaces": flat.get("interfaces") or [],
        "datastores": flat.get("datastores") or [],
    }
    (root / paths.INTERFACES).write_text(
        yaml.safe_dump(contracts, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    root_index = {
        key: value
        for key, value in flat.items()
        if key not in {"modules", "interfaces", "datastores"}
    }
    root_index["modules"] = entries
    (root / paths.PROJECT_MAP).write_text(
        yaml.safe_dump(root_index, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return root


def patch_module_map(root: Path, module_id: str, **fields: Any) -> Path:
    """改一个模块的模块地图。

    「这个模块怎么跑」在树里住模块地图,不在根索引。夹具往根索引里塞 `test_cmd` 会被
    严格模式拒收——而那报出来像是拼写错误,不像是"字段搬家了"。
    """
    target = root / paths.MODULES / module_id / MODULE_MAP
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    payload.update(fields)
    target.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return target


def module_ids(root: Path) -> list[str]:
    """根索引里有哪些模块。"""
    payload = yaml.safe_load((root / paths.PROJECT_MAP).read_text(encoding="utf-8")) or {}
    return [str(entry["id"]) for entry in payload.get("modules") or []]


def patch_contracts(root: Path, **sections: Any) -> Path:
    """改契约索引(跨模块契约与数据存储)。它们不再住根索引。"""
    target = root / paths.INTERFACES
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    payload.update(sections)
    target.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return target


__all__ = ["module_ids", "patch_contracts", "patch_module_map", "write_flat_as_tree"]
