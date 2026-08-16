"""守卫:全代码库只许有一套图模型。

## 它从"别再长出来"变成了"确实没有了"

仓里曾经有两套图:PRD 14 留下的**子任务图**(按子任务建模、自带环校验、调度器与超时)与
`core.topology` 的**执行拓扑**。dag 执行器落地时把前者删掉了——两套并存的代价不是多几个
文件,是**环检测、调度、超时各有两份实现,然后在某个细节上悄悄分叉**。

它的两条判断没有跟着代码一起消失:① 环必须在**加载那一刻**拒绝(见 `topology_validate`
的环检测);② 同仓串行不是性能优化而是正确性(见写集冲突检验的注释——那条规则按仓判是它
的粗粒度先祖)。**删代码不删判断**,是这次清理最容易犯的错。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

#: 已经删掉的旧图模块。**任何一处再 import 它们都是复活。**
DELETED = {"agentgenome.core.graph", "agentgenome.core.scheduler"}


def importers_of_the_old_graph() -> set[str]:
    found = set()
    for directory in ("src", "tests"):
        for path in (ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if module in DELETED:
                    found.add(str(path.relative_to(ROOT)))
    return found


def test_the_old_subtask_graph_is_really_gone() -> None:
    assert importers_of_the_old_graph() == set()
    for name in ("graph", "scheduler"):
        assert not (ROOT / "src" / "agentgenome" / "core" / f"{name}.py").exists()


def test_the_two_judgements_it_carried_survive_as_behaviour() -> None:
    """删代码不删判断——而判断要用**行为**来验,不是断言注释里的措辞。

    断言散文的守卫会在有人改一句话时红,在有人删掉那条规则时绿——两次都错。
    """
    from agentgenome.core.topology import DAG, TopologyNode, TopologyTemplate
    from agentgenome.core.topology_validate import CYCLE, WRITE_CONFLICT, validate_topology

    cyclic = TopologyTemplate(
        id=DAG,
        nodes=(
            TopologyNode(id="a", needs=("b.out",), produces=("a.out",)),
            TopologyNode(id="b", needs=("a.out",), produces=("b.out",)),
        ),
        edges=(("a", "b"), ("b", "a")),
    )
    same_repo = TopologyTemplate(
        id=DAG,
        nodes=(
            TopologyNode(id="a", write_scope=("repos/order/**",)),
            TopologyNode(id="b", write_scope=("repos/order/src/*.py",)),
        ),
    )

    # ① 环在**加载那一刻**被拒(而不是等调度时静默死锁)。
    assert [issue.code for issue in validate_topology(cyclic)] == [CYCLE]
    # ② 同仓串行的细化版:两个并行节点写同一个仓即拒。
    assert [issue.code for issue in validate_topology(same_repo)] == [WRITE_CONFLICT]
