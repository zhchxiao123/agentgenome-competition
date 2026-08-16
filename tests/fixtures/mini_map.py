"""一份最小项目地图,以及摆出它的 Workspace。

三个测试文件各摆一遍的话,"项目地图长什么样"就有了三个略有出入的版本。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentgenome.genome.models import ProjectMap
from agentgenome.genome.tree import write_tree

PROJECT_MAP: dict[str, Any] = {
    "version": 1,
    "project": {"name": "mall", "summary": ""},
    "modules": [
        {
            "id": "order-service",
            "path": "repos/order-service/",
            "test_cmd": "pytest -q",
            "build_cmd": "make build",
            "junit_xml_path": "reports/junit.xml",
        },
        {"id": "inventory-service", "path": "repos/inventory-service/"},
    ],
}


def write_workspace(root: Path, project_map: dict[str, Any] | None = None) -> Path:
    """摆一个只有项目地图的 Workspace。门禁配置那几层由各测试自己加。"""
    (root / "genome" / "knowledge").mkdir(parents=True, exist_ok=True)
    payload = project_map or PROJECT_MAP
    for module in payload["modules"]:
        (root / module["path"].rstrip("/")).mkdir(parents=True, exist_ok=True)
    # 摆的是树而不是单文件:夹具要和产品代码写出来的形状一致,否则测试通过的是一个
    # 系统里不存在的布局。
    write_tree(root, ProjectMap.model_validate(payload))
    return root
