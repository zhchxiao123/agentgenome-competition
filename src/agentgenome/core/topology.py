"""执行拓扑:状态内部那张图的数据形状。

## 状态机是治理模型,拓扑层是协作模型

状态机管的是"什么算完成、谁来批准、烧了多少钱、失败了退到哪"——这是生产可信的底线,永远
不该为灵活性让步。真正单调的地方在状态**内部**:今天每个状态恰好等于「一个员工 × 一次
线性尝试」。所以这一层的立场是**状态机在外、图在内**:模板描述状态内部怎么跑,聚合之后
抛出的仍然是既有的状态机事件,外环零改动。

## 为什么模板是数据而不是代码

图长什么样必须**事后可回放**。写成代码的话,"这次到底跑了哪张图"只能靠读日志猜;写成数据
之后,它可以整份进事件面,前端画图的数据源就是它,而且可以在派发前被一个纯函数判死
(见 `topology_validate`)。

## 节点即 Job,不是子任务

一个节点 = 一次 Job = 一个员工 × 一道工序 × 一个上下文包。**不新增 Task 层级**:任务仍是
一个,一个分支、一个 PR 面向审批人——拆成子任务树会把"任务全貌"切碎到审批人拼不回来。

## 数据形状冻结

后续执行器 PRD **不得修改**这里的字段。需要新字段即说明这个模型没定对,回来改这里并在
ADR 里说明理由。`Termination` 与 `Variant` 本期没有任何执行器消费,仍然现在定义:环的收敛
判据与每路变体的取向在后续 PRD 里都需要落点,而在一个已经声明冻结的结构里它们没有位置
——那等于在写下承诺的同一处预定了它的破产。定义的成本是两个 dataclass。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

#: 内置模板 id。**只有内置,不做用户自定义图**——我们不重新发明 BPMN。
SINGLE = "single"
CRITIQUE_LOOP = "critique-loop"
ASSISTED = "assisted"
DAG = "dag"
BEST_OF_N = "best-of-n"
TEST_FIRST = "test-first"
PROBE_AFTER_GATE = "probe-after-gate"

#: `stop_when` 允许的形状:一个产物字段名,不是表达式。校验器与执行器共用这一条。
FIELD_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


class NodeKind(StrEnum):
    """节点产出什么。

    **两者都产出,区别在产出物的去处。** work 产出资产(进版本面:代码、文档、卡片),
    checker 产出判定(只进产物面与事件面)。把 checker 定义成"只评估不生产"的话,
    `critique(checker) → refine` 这条靠 critique.json 背书的边会被自家校验器判成假边——
    规则与模板必须能同时成立。
    """

    WORK = "work"
    CHECKER = "checker"


class Executor(StrEnum):
    """这个节点由谁执行。

    **没有第三个取值。** assisted 是组合(auto 节点 + 人的确认节点),不是新原语:做成
    第三个取值的话,每个执行器里都要长出一条"这个节点是半自动的"分支,而组合表达它只需要
    多一个节点。
    """

    AUTO = "auto"
    MANUAL = "manual"


class TopologyParseError(ValueError):
    """模板数据不合法。

    **未知的键一律拒绝,不静默吞掉。** 吞掉的话,一个 `write_scopes:` 的笔误会变成"这个
    节点没声明写集",而写集正是并行安全的判据——症状要到两个节点互相覆盖时才出现。
    """


@dataclass(frozen=True)
class Termination:
    """环的终止判据。**没有它就不许有环。**

    反馈环不走依赖边(依赖成环是静默死锁),它由这里表达——所以环检测拦的是"依赖成环",
    不是"迭代"。三条判据任一触顶即终止,`budget_share` 是其中最容易被漏掉的一条:轮次
    封顶管得住次数,管不住单轮的花费,而"归因"不是"封顶"。
    """

    #: 硬上限,任何情况下的兜底。
    max_rounds: int = 2
    #: 收敛判据:**产物里的一个布尔字段名**(如 `approved`),不是表达式。
    #: `None` 表示只看轮次与预算。写成 `critique.approved == true` 这类表达式会被校验器拒绝
    #: ——理由见 `_converged`:一个只有内置模板用得上的小语言不值得发明。
    stop_when: str | None = None
    #: 整个环占任务预算的份额。0 表示不额外分摊。
    budget_share: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "max_rounds": self.max_rounds,
            "stop_when": self.stop_when,
            "budget_share": self.budget_share,
        }


@dataclass(frozen=True)
class Variant:
    """同一节点的一路变体。

    变异靠取向提示,不靠新角色——`hint` 进上下文包,`key` 进产物编址与事件面。胜者的取向
    本身就是一条可蒸馏的经验("这类任务上'最小改动'赢了两次")。
    """

    key: str
    hint: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {"key": self.key, "hint": self.hint}


@dataclass(frozen=True)
class TopologyNode:
    """一个节点 = 一次 Job。"""

    id: str
    kind: NodeKind = NodeKind.WORK
    #: 以谁的身份干。**与 procedure 分开**:同一道工序换个员工就是另一种权限与工具集,
    #: 而评审员工正是"同一批代码、只读权限"的那一个。
    employee: str = ""
    procedure: str = ""
    executor: Executor = Executor.AUTO
    #: manual 节点的人(或角色,经 RBAC 解析)。
    assignee: str | None = None
    #: 消费的产物名(来自上游节点的 produces)。
    needs: tuple[str, ...] = ()
    #: 产出的产物名。经产物总线,与 manifest 同源。
    produces: tuple[str, ...] = ()
    #: 声明写集(glob)。校验器判并行安全的依据。
    write_scope: tuple[str, ...] = ()
    budget_share: float = 1.0
    #: 空 = 单路;非空 = N 路变体。
    variants: tuple[Variant, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "employee": self.employee,
            "procedure": self.procedure,
            "executor": self.executor.value,
            "assignee": self.assignee,
            "needs": list(self.needs),
            "produces": list(self.produces),
            "write_scope": list(self.write_scope),
            "budget_share": self.budget_share,
            "variants": [item.to_payload() for item in self.variants],
        }


@dataclass(frozen=True)
class TopologyTemplate:
    """一张图。`single` 是它的平凡实例。"""

    id: str
    nodes: tuple[TopologyNode, ...] = ()
    #: 依赖边。**边必须由产物背书**(见 `topology_validate`),single 为空。
    edges: tuple[tuple[str, str], ...] = ()
    termination: Termination | None = None
    #: 裁决策略。single/dag 不需要。
    selection: str | None = None

    def node(self, node_id: str) -> TopologyNode:
        for item in self.nodes:
            if item.id == node_id:
                return item
        raise KeyError(node_id)

    def to_payload(self) -> dict[str, Any]:
        """整份进事件面的那个形状。记进去读不回来的话,回放就断了。"""
        return {
            "id": self.id,
            "nodes": [item.to_payload() for item in self.nodes],
            "edges": [list(edge) for edge in self.edges],
            "termination": self.termination.to_payload() if self.termination else None,
            "selection": self.selection,
        }


def single_template(
    employee: str,
    procedure: str,
    node_id: str = "main",
    executor: Executor = Executor.AUTO,
) -> TopologyTemplate:
    """现状那条路的模板形态:单节点、无边、无变体、无终止判据。

    它平凡通过全部校验,而且塌缩回今天那一次派发——`single` 等价性是硬验收,不是希望。
    """
    return TopologyTemplate(
        id=SINGLE,
        nodes=(
            TopologyNode(
                id=node_id, employee=employee, procedure=procedure, executor=executor
            ),
        ),
    )


#: 确认节点的名字。**assisted 是组合,不是新原语**:一个自动节点 + 一个人的确认节点。
#: 做成第三种执行者取值的话,每个执行器里都要长出一条"这个节点是半自动的"分支;而组合
#: 表达它只需要多一个节点,并且那个节点自动获得待办、超时三段、改派、RBAC 全套设施。
CONFIRM = "confirm"

#: best-of-n 里三个节点的名字。
ATTEMPT = "attempt"
GATE = "gate"
JUDGE = "judge"

#: 环里三个节点的名字。**进产物编址与事件面**,所以它们是接口的一部分,不是局部变量。
GENERATE = "generate"
CRITIQUE = "critique"
REFINE = "refine"

#: 环里流动的两份产物。名字只在图内部有意义:边靠它们背书。
_WORK_ARTIFACT = "work-result"
_CONFIRM_ARTIFACT = "confirmation"
_FITNESS_ARTIFACT = "fitness"
_VERDICT_ARTIFACT = "verdict"
_CRITIQUE_ARTIFACT = "critique.json"


def dag_template(
    employee: str, procedure: str, nodes: Sequence[Mapping[str, Any]]
) -> TopologyTemplate:
    """把计划产出的节点集变成一张图。

    **边由产物推出来,不由声明的顺序推出来**:edge (A→B) 存在当且仅当 B 消费了 A 产出的
    某样东西。这样"先后顺序"就没有地方可以偷偷变成一条依赖——它是 ADR-0007 在产图这一侧的
    落点,也让"假边"从一种约定变成一件不可能画出来的事。
    """
    parsed = tuple(
        TopologyNode(
            id=str(item["id"]),
            employee=employee,
            procedure=procedure,
            needs=tuple(str(x) for x in item.get("needs", ())),
            produces=tuple(str(x) for x in item.get("produces", ())),
            write_scope=tuple(str(x) for x in item.get("write_scope", ())),
        )
        for item in nodes
    )
    edges = tuple(
        (upstream.id, downstream.id)
        for upstream in parsed
        for downstream in parsed
        if upstream.id != downstream.id and set(downstream.needs) & set(upstream.produces)
    )
    return TopologyTemplate(id=DAG, nodes=parsed, edges=edges)


def best_of_n_template(
    employee: str,
    procedure: str,
    gate_procedure: str,
    judge_employee: str,
    judge_procedure: str,
    variants: Sequence[Variant],
) -> TopologyTemplate:
    """`attempt(N 路) → gate(N 路) → judge` 的那张图。

    **门禁即适应度,不是评审偏好**:每一路各自过门禁,judge 只在过闸集合里选。一个跑不过
    测试的方案再优雅也出局,而这条判据是确定性的、不烧 token、不看脸色。

    N 路变体按设计写同一批路径(它们是同一件事的不同做法)——写集冲突检验对同一节点的不同
    变体豁免,靠分支物理隔离,而且只有一路会被合并。
    """
    if len(variants) < 2:
        raise UnknownTopology(f"{BEST_OF_N} 至少要两路变体,一路的话它就是 single")
    return TopologyTemplate(
        id=BEST_OF_N,
        nodes=(
            TopologyNode(
                id=ATTEMPT,
                employee=employee,
                procedure=procedure,
                produces=(_WORK_ARTIFACT,),
                variants=tuple(variants),
            ),
            TopologyNode(
                id=GATE,
                kind=NodeKind.CHECKER,
                employee=employee,
                procedure=gate_procedure,
                needs=(_WORK_ARTIFACT,),
                produces=(_FITNESS_ARTIFACT,),
                variants=tuple(variants),
            ),
            TopologyNode(
                id=JUDGE,
                kind=NodeKind.CHECKER,
                employee=judge_employee,
                procedure=judge_procedure,
                needs=(_FITNESS_ARTIFACT,),
                produces=(_VERDICT_ARTIFACT,),
            ),
        ),
        edges=((ATTEMPT, GATE), (GATE, JUDGE)),
        selection=JUDGE,
    )


def assisted_template(
    employee: str, procedure: str, assignee: str = "", node_id: str = "main"
) -> TopologyTemplate:
    """`work(auto) → confirm(human)` 的那张图。

    确认节点是一个极小的人工作业:输入是待确认产物的摘要,输出是"通过没有 + 意见"。
    它是 `checker`——产出判定而不是资产,所以不声明写集。
    """
    return TopologyTemplate(
        id=ASSISTED,
        nodes=(
            TopologyNode(
                id=node_id, employee=employee, procedure=procedure, produces=(_WORK_ARTIFACT,)
            ),
            TopologyNode(
                id=CONFIRM,
                kind=NodeKind.CHECKER,
                employee=employee,
                procedure=procedure,
                executor=Executor.MANUAL,
                assignee=assignee or None,
                needs=(_WORK_ARTIFACT,),
                produces=(_CONFIRM_ARTIFACT,),
            ),
        ),
        edges=((node_id, CONFIRM),),
        termination=Termination(max_rounds=1, stop_when="approved"),
    )


#: test-first 两个节点的名字。进产物编址与事件面,所以它们是接口的一部分。
WRITE_TESTS = "write-tests"
IMPLEMENT = "implement"

#: 出题人交给实现人的那份东西。边靠它背书——**不是"先后顺序"**。
_TESTS_ARTIFACT = "test-cases"


def test_first_template(
    tester: str,
    tester_procedure: str,
    developer: str,
    developer_procedure: str,
) -> TopologyTemplate:
    """`write-tests(红) → implement(绿)` 的那张图。

    **它是顺序模板,不是 dag 的一个预设形状。** dag 的每个节点在自己的工作树里干活,而合并
    发生在整张图跑完之后——做成 dag 的话,实现节点的工作树里根本没有测试员工刚写的用例文件,
    它只能从上游拿到一份小票,红→绿在文件层面不会发生。所以这张图与环、assisted 同族:
    **共用任务主工作树、不分节点工作树、不做节点合并**。

    也正因为共用一棵树,写集分离只能靠越权机制来做,靠不了工作树隔离。

    两个节点都是 `work`:出题产出用例文件,实现产出代码,都是进版本面的资产。状态机事件由
    最后一个工作节点决定,于是"这个状态干成了没有"仍然由实现那一步回答——与今天一致。
    """
    return TopologyTemplate(
        id=TEST_FIRST,
        nodes=(
            TopologyNode(
                id=WRITE_TESTS,
                employee=tester,
                procedure=tester_procedure,
                produces=(_TESTS_ARTIFACT,),
            ),
            TopologyNode(
                id=IMPLEMENT,
                employee=developer,
                procedure=developer_procedure,
                needs=(_TESTS_ARTIFACT,),
                produces=(_WORK_ARTIFACT,),
            ),
        ),
        edges=((WRITE_TESTS, IMPLEMENT),),
    )


#: 对抗链两个节点的名字。
UNIT_GATE = "unit-gate"
PROBE = "probe"

#: 门禁交给红队的那份东西:一份"这一轮改了什么、测试都绿了"的结论。边靠它背书。
_GATE_ARTIFACT = "gate-report"
_FINDINGS_ARTIFACT = "findings"


def probe_after_gate_template(
    gate_employee: str,
    gate_procedure: str,
    adversary: str,
    probe_procedure: str,
) -> TopologyTemplate:
    """`unit-gate → probe` 的那张图。**编在门禁之后、集测判定之前。**

    对抗节点是 **work 而不是 checker**,这一条最容易做错:状态机事件由最后一个**工作**节点
    的产物决定,检查节点只影响环内走向——而"影响环内走向"只存在于精化环的执行器里。标成
    checker 的话,红队抓到真 bug、写下 `passed: false`,状态照样抛门禁通过。

    它标成 work 在词汇上也站得住:它**确实产出资产**(攻击脚本与用例文件),findings 是那些
    资产的判定副产品。

    顺序模板而非 dag,除了"下游要读上游写的东西"这条通用理由,还有一条专属的:dag 的节点
    工作树从工作区 HEAD 分叉,而对抗要看的是**本任务分支上的改动**。
    """
    return TopologyTemplate(
        id=PROBE_AFTER_GATE,
        nodes=(
            TopologyNode(
                id=UNIT_GATE,
                employee=gate_employee,
                procedure=gate_procedure,
                produces=(_GATE_ARTIFACT,),
            ),
            TopologyNode(
                id=PROBE,
                employee=adversary,
                procedure=probe_procedure,
                needs=(_GATE_ARTIFACT,),
                produces=(_FINDINGS_ARTIFACT,),
            ),
        ),
        edges=((UNIT_GATE, PROBE),),
    )


def critique_loop_template(
    employee: str,
    procedure: str,
    reviewer: str,
    critique_procedure: str,
    max_rounds: int = 2,
    budget_share: float = 0.0,
) -> TopologyTemplate:
    """`generate → critique → refine` 的那张图。

    批判节点是 `checker`:它产出判定(`critique.json`)而不是资产,所以它不声明写集——
    一个能改代码的批判者会不自觉地把批判改成迁就。而它**确实产出**,否则
    `critique → refine` 这条边会被自家校验器判成假边。
    """
    return TopologyTemplate(
        id=CRITIQUE_LOOP,
        nodes=(
            TopologyNode(
                id=GENERATE,
                employee=employee,
                procedure=procedure,
                produces=(_WORK_ARTIFACT,),
            ),
            TopologyNode(
                id=CRITIQUE,
                kind=NodeKind.CHECKER,
                employee=reviewer,
                procedure=critique_procedure,
                needs=(_WORK_ARTIFACT,),
                produces=(_CRITIQUE_ARTIFACT,),
            ),
            TopologyNode(
                id=REFINE,
                employee=employee,
                procedure=procedure,
                needs=(_CRITIQUE_ARTIFACT,),
                produces=(_WORK_ARTIFACT,),
            ),
        ),
        edges=((GENERATE, CRITIQUE), (CRITIQUE, REFINE)),
        termination=Termination(
            max_rounds=max_rounds, stop_when="approved", budget_share=budget_share
        ),
    )


_NODE_KEYS = frozenset(
    {
        "id",
        "kind",
        "employee",
        "procedure",
        "executor",
        "assignee",
        "needs",
        "produces",
        "write_scope",
        "budget_share",
        "variants",
    }
)
_TEMPLATE_KEYS = frozenset({"id", "nodes", "edges", "termination", "selection"})
_TERMINATION_KEYS = frozenset({"max_rounds", "stop_when", "budget_share"})
_VARIANT_KEYS = frozenset({"key", "hint"})


def _refuse_unknown(payload: dict[str, Any], allowed: frozenset[str], what: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise TopologyParseError(f"{what} 有未知字段: {', '.join(unknown)}")


def _sequence(raw: Any, what: str) -> tuple[Any, ...]:
    """一个数组字段。**字符串不算数组**——`needs: spec.md` 会被当成逐字符的列表,
    然后校验器报"节点消费 s、p、e…",没人看得懂那是 YAML 少写了一对方括号。
    """
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise TopologyParseError(f"{what} 必须是数组")
    return tuple(raw)


def _strings(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(item) for item in _sequence(payload.get(key, ()), key))


def _enum(value: Any, enum: type[StrEnum], key: str) -> Any:
    try:
        return enum(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in enum)
        raise TopologyParseError(f"{key} 只能是 {allowed},收到 {value!r}") from error


def parse_variant(payload: dict[str, Any]) -> Variant:
    _refuse_unknown(payload, _VARIANT_KEYS, "variant")
    if "key" not in payload:
        raise TopologyParseError("variant 缺 key")
    return Variant(key=str(payload["key"]), hint=str(payload.get("hint", "")))


def parse_node(payload: dict[str, Any]) -> TopologyNode:
    _refuse_unknown(payload, _NODE_KEYS, "节点")
    if "id" not in payload:
        raise TopologyParseError("节点缺 id")
    variants = _sequence(payload.get("variants", ()), "variants")
    return TopologyNode(
        id=str(payload["id"]),
        kind=_enum(payload.get("kind", NodeKind.WORK), NodeKind, "kind"),
        employee=str(payload.get("employee", "")),
        procedure=str(payload.get("procedure", "")),
        executor=_enum(payload.get("executor", Executor.AUTO), Executor, "executor"),
        assignee=payload.get("assignee"),
        needs=_strings(payload, "needs"),
        produces=_strings(payload, "produces"),
        write_scope=_strings(payload, "write_scope"),
        budget_share=float(payload.get("budget_share", 1.0)),
        variants=tuple(parse_variant(dict(item)) for item in variants),
    )


def parse_termination(payload: dict[str, Any]) -> Termination:
    _refuse_unknown(payload, _TERMINATION_KEYS, "termination")
    return Termination(
        max_rounds=int(payload.get("max_rounds", 2)),
        stop_when=payload.get("stop_when"),
        budget_share=float(payload.get("budget_share", 0.0)),
    )


def parse_template(payload: dict[str, Any]) -> TopologyTemplate:
    """从数据读回一张图。

    **严格解析**:未知字段、错类型、形状不对的边一律当场拒绝——但**图对不对是校验器的事**
    (`topology_validate`)。分开是因为它们的失败面不同:这里答"这份数据是不是一张图",
    那里答"这张图跑不跑得动";混在一起的话,一个语法错误会以"假边"的名义报出来。
    """
    if not isinstance(payload, dict):
        raise TopologyParseError("模板必须是一个映射")
    _refuse_unknown(payload, _TEMPLATE_KEYS, "模板")
    if "id" not in payload:
        raise TopologyParseError("模板缺 id")

    raw_nodes = _sequence(payload.get("nodes", ()), "nodes")

    edges: list[tuple[str, str]] = []
    for edge in _sequence(payload.get("edges", ()), "edges"):
        if isinstance(edge, str) or len(tuple(edge)) != 2:
            raise TopologyParseError(f"边必须是 [上游, 下游] 两元组,收到 {edge!r}")
        upstream, downstream = tuple(edge)
        edges.append((str(upstream), str(downstream)))

    termination = payload.get("termination")
    return TopologyTemplate(
        id=str(payload["id"]),
        nodes=tuple(parse_node(dict(item)) for item in raw_nodes),
        edges=tuple(edges),
        termination=parse_termination(dict(termination)) if termination else None,
        selection=payload.get("selection"),
    )


@dataclass(frozen=True)
class NodeOutcome:
    """一个节点跑完之后的结论。

    **执行器只搬运它,不解释它。** 判定"这算不算通过"属于状态处理器:执行器懂的是图,
    不懂业务语义——让它去读 `passed` 字段的话,每加一种模板就要复制一遍那段判定。

    唯一的例外是**花费**:环级预算是终止判据之一,而终止是执行器的事,所以用量得跟着
    结论一起回来。拿不到用量时标 `tokens_available=False`,**不填 0**——填 0 的话,
    一个拿不到用量的运行时会让环级预算永远花不完,而那正是它该拦住的那种失控。
    """

    node: TopologyNode
    ok: bool
    payload: dict[str, Any] | None = None
    tokens_used: int = 0
    tokens_available: bool = True
    #: 这个节点**挂起了**:活派出去了,结果要等人回来。执行器见到它就停,不往下跑——
    #: 环里的下一步要读上一步的产物,而挂起时那份产物还不存在。
    suspended: bool = False
    #: 挂起在等哪张待办。
    todo_id: str = ""
    #: 这个节点的产物目录名。**事件面只记这个指针,不记产物内容**——同一件事由一个平面
    #: 记录内容、别的平面记指针,两个平面各存一份必然发散,而发散之后没办法判断哪份是对的。
    slot: str = ""
    #: Job 失败的机器可判定类别。拓扑层不解释它，只把它带给状态处理器决定是重做还是升级。
    failure_kind: str = ""
    failure_detail: str = ""


#: 环停下来的原因。**"为什么停"必须记下来**:跑满轮次停和收敛停是两种完全不同的信号,
#: 而它们正是事后判断"这个环值不值"的唯一依据。
STOPPED_CONVERGED = "converged"
STOPPED_MAX_ROUNDS = "max-rounds"
STOPPED_BUDGET = "budget"
STOPPED_CHECKER_FAILED = "checker-failed"
#: 图里有节点失败了。**它不是"图停了"**:旁支跑完了,下游冻结了,而这一轮的结论要由
#: 处理器按"关键路径上有没有失败"来定。
STOPPED_NODE_FAILED = "node-failed"
#: 人确认时拒了。**它不是失败**:产出是好的还是差的由下一轮回答,这一条只说"先别往下走"。
STOPPED_REJECTED = "rejected"
#: 环停在了一个派给人的节点上。**不是终止,是等**——人一交活,这个状态会被重新推一步。
STOPPED_SUSPENDED = "suspended"


@dataclass(frozen=True)
class TopologyRun:
    """一次拓扑执行的全部结果。"""

    outcomes: tuple[NodeOutcome, ...]
    #: 环为什么停。非环模板留空。
    stopped_because: str = ""
    #: 跑了几轮检查。非环模板是 0。
    rounds: int = 0
    tokens_used: int = 0
    tokens_available: bool = True
    #: 哪些节点失败了。
    failed: tuple[str, ...] = ()
    #: 胜出的那一路变体(best-of-n)。空串表示这次没有变体维度。
    winner: str = ""
    #: 过了闸的那些变体。**落选的不丢弃**:它们是"为什么不这么做"的一手证据。
    survivors: tuple[str, ...] = ()
    #: 哪些节点因为上游失败而**根本没跑**。与失败分开:被冻结的节点没有失败现场,
    #: 把它们算成失败会让下一轮的诊断材料里多出几条不存在的失败。
    frozen: tuple[str, ...] = ()

    def last_work(self) -> NodeOutcome | None:
        """最后一个**工作**节点的结论。

        **状态机事件由工作节点决定,检查节点只影响环的走向。** 直接取最后一个结论的话,
        环的最后一步常常是一次批判,于是"这个状态干成了没有"会由批判的产物来回答——
        而批判压根不产出资产。
        """
        for outcome in reversed(self.outcomes):
            if outcome.node.kind is NodeKind.WORK:
                return outcome
        return None


@dataclass(frozen=True)
class TemplateChoice:
    """选中的模板,以及**为什么是它**。

    理由跟着模板一起走:它要进事件面。事后判断"这个策略值不值"的第一个问题就是"这次为什么
    进了环",而那个答案只在选模板的那一刻存在过一次——不带出来就永远回答不了。
    """

    template: TopologyTemplate
    #: 命中的策略名。空串表示没命中(跑的是 `single`)。
    why: str = ""


@dataclass(frozen=True)
class ExecutionLimits:
    """执行器需要知道的外部约束。

    任务预算是环级预算份额的分母——执行器算不出绝对上限,除非有人把它告诉它。
    """

    budget_tokens: int | None = None
    #: 同时最多跑几个节点。**并行的钱不是新钱**:同一个任务预算,花得更快也停得更准,
    #: 而机器与 API 配额是有限的——不设上限的图会把两者一起打爆。
    max_parallel: int = 3


#: 跑一个节点。由派发方给,执行器自己不知道 Job 是什么。
NodeRunner = Callable[[TopologyNode], Awaitable[NodeOutcome]]
#: 一个执行器:拿一张图、一个节点跑法与外部约束,按自己的调度语义跑完它。
TopologyExecutor = Callable[[TopologyTemplate, NodeRunner, ExecutionLimits], Awaitable[TopologyRun]]


class UnknownTopology(LookupError):
    """请求的模板没有执行器,或者模板数据不符合那个执行器的形状。

    **不静默回退到 single。** 回退的话,一个配成 `dag` 的部署会安安静静地按单节点跑,
    表现为"并行怎么没生效"——而原因在三层之外的一张注册表里。
    """


class TopologyRefused(ValueError):
    """图本身跑不动:校验没过。

    **与"没有这个模板"分成两个异常。** 合成一个的话,调用方无法区分"配置写错了"与
    "这次拆出来的图不合法"——前者要改配置,后者要重拆图,而重拆图那条路会消耗
    需求解析重试。一个异常回答不了"下一步该干什么"。
    """

    def __init__(self, issues: tuple[str, ...]) -> None:
        self.issues = issues
        super().__init__("图校验未通过:" + "; ".join(issues))


async def _run_single(
    template: TopologyTemplate, run_node: NodeRunner, limits: ExecutionLimits
) -> TopologyRun:
    """`single`:跑那唯一一个节点。

    它存在的意义不是调度,而是**让泛化后的路径与现状逐字节一致**:处理器不再自己派 Job,
    但派出去的仍然是同一个 Job。
    """
    if len(template.nodes) != 1 or template.edges:
        raise UnknownTopology(f"single 模板只能有一个节点、没有边: {template.id}")
    outcome = await run_node(template.nodes[0])
    # single 只负责确认这一轮有没有可交给状态处理器解释的产物。`ok=False` 不一定表示
    # 没有结论：例如集成测试的 Agent 诊断可失败，但确定性脚本写下的测试报告仍是事实。
    # 没有任何产物时才是拓扑节点失败，否则由 handler 按工序语义裁决。
    failed = (outcome.node.id,) if not outcome.suspended and outcome.payload is None else ()
    return TopologyRun(
        outcomes=(outcome,),
        stopped_because=STOPPED_NODE_FAILED if failed else "",
        tokens_used=outcome.tokens_used,
        tokens_available=outcome.tokens_available,
        failed=failed,
    )


def _sequence_executor(verdict_ends_the_state: bool) -> TopologyExecutor:
    """一条链:按声明顺序跑,**前一步没成就不跑下一步**。

    与 `dag` 的区别不在形状(一条链当然也是 dag),在**空间**:这里的节点共用任务主工作树,
    所以下一个节点看得见上一个写下的文件。dag 的节点各有工作树、合并发生在全图跑完之后,
    于是 `[出题] → [实现]` 这种"下游要读上游写的文件"的链在 dag 上根本不成立。

    ## 参数在回答一个问题:节点说"没通过"时,那是**这一轮没干成**,还是**这个状态的结论**?


    - `False`(前置链,如 test-first):出题没出出来 → 这一轮不算,原地再来一轮。它不该被
      当成状态的结论往下走——那会让一个什么都没实现的任务走到门禁面前。
    - `True`(判定链,如门禁 → 对抗):门禁没过、或者对抗抓到了东西,**那本来就是这个状态
      要给出的答案**。报成"节点失败"的话,任务会原地重跑同一份没改过的代码,直到轮次耗尽
      ——而它本该带着失败报告回到开发态。

    两种语义共用一份调度代码,差别只有这一个判断;各写一个执行器的话,"失败即停"这条会被
    实现两遍,然后其中一遍迟早忘了冻结下游。
    """

    async def run(
        template: TopologyTemplate, run_node: NodeRunner, limits: ExecutionLimits
    ) -> TopologyRun:
        if not template.nodes:
            raise UnknownTopology(f"顺序模板至少要有一个节点: {template.id}")

        outcomes: list[NodeOutcome] = []
        failed: list[str] = []
        frozen: list[str] = []
        for index, node in enumerate(template.nodes):
            outcome = await run_node(node)
            outcomes.append(outcome)
            if outcome.suspended:
                # 人还没交活。**短路**:再往下跑的话,下一个节点会读到一份还不存在的产物。
                break
            if _node_ok(outcome):
                continue
            rest = [item.id for item in template.nodes[index + 1 :]]
            # 判定链上,Job 本身挂了(契约不符、越权、进程死)仍然是节点失败——那不是一个
            # 结论,那是没能给出结论。只有"跑成了但说没通过"才是结论。
            if not verdict_ends_the_state or not outcome.ok:
                failed.append(node.id)
            frozen.extend(rest)
            break
        return TopologyRun(
            outcomes=tuple(outcomes),
            failed=tuple(failed),
            frozen=tuple(frozen),
            tokens_used=sum(item.tokens_used for item in outcomes),
            tokens_available=all(item.tokens_available for item in outcomes),
        )

    return run


def _node_ok(outcome: NodeOutcome) -> bool:
    """这个节点算干成了没有。

    **两个都要看**:Job 本身成了,而且产物自己说通过了。只看前者的话,一个写了
    `passed: false` 的出题节点会被当成成功,实现节点接着在一份"我没写出用例"的产物上干活。
    """
    return outcome.ok and bool((outcome.payload or {}).get("passed", False))


def loop_nodes(
    template: TopologyTemplate,
) -> tuple[TopologyNode, TopologyNode, TopologyNode]:
    """环的三个角色:生成、批判、精化。

    **形状写死,不猜。** 通用循环要一套图灵完备的控制流,而我们只需要"生成一次、批判到
    收敛";形状不对就拒绝执行,与 `single` 拒绝多节点模板同一个理由——执行器只跑它懂的
    那种图。
    """
    kinds = tuple(node.kind for node in template.nodes)
    if kinds != (NodeKind.WORK, NodeKind.CHECKER, NodeKind.WORK):
        raise UnknownTopology(
            f"{CRITIQUE_LOOP} 模板必须是三个节点:生成(work)、批判(checker)、精化(work);"
            f"收到 {[item.value for item in kinds]}"
        )
    seed, checker, refine = template.nodes
    return seed, checker, refine


def _converged(outcome: NodeOutcome, stop_when: str | None) -> bool:
    """批判说通过了没有。

    `stop_when` 是**产物里的一个布尔字段名**,不是表达式。写一个能解析
    `critique.approved == true` 的小语言,等于在这里发明第四种配置语法,而它唯一的用户是
    我们自己写的内置模板。不是纯字段名时当场拒绝,而不是当成"没通过"静默跑满轮次——
    后者的症状是"这个环怎么每次都跑满两轮",而原因在一个拼错的谓词里。
    """
    if stop_when is None:
        return False
    if not FIELD_NAME.match(stop_when):
        raise TopologyRefused((f"stop_when 只支持产物里的一个布尔字段名,收到 {stop_when!r}",))
    return bool((outcome.payload or {}).get(stop_when, False))


async def _run_critique_loop(
    template: TopologyTemplate, run_node: NodeRunner, limits: ExecutionLimits
) -> TopologyRun:
    """`critique-loop`:生成一次,然后 (批判, 精化) 反复,每次批判之后判一次终止。

    三条终止判据任一触顶即停,而三种停法的**出路完全相同**:带最后一版工作产出继续往下走,
    不升级人工——环不是闸门,它是增值步骤。
    """
    seed, checker, refine = loop_nodes(template)
    termination = template.termination or Termination()
    ceiling = _ceiling(termination, limits)

    outcomes = [await run_node(seed)]
    if outcomes[0].suspended:
        # 生成这一步派给了人:环就停在这儿等他。不停的话,批判会去读一份还不存在的产物。
        return TopologyRun(outcomes=tuple(outcomes), stopped_because=STOPPED_SUSPENDED)
    if not outcomes[0].ok or outcomes[0].payload is None:
        # 没有有效工作产出就没有可评审对象。继续拉 checker 只会多花一次调用，且它的
        # 结论没有事实基础。
        return TopologyRun(
            outcomes=tuple(outcomes),
            stopped_because=STOPPED_NODE_FAILED,
            failed=(seed.id,),
            tokens_available=outcomes[0].tokens_available,
        )
    # **生成那一次不算进环的账。** 它是这个状态本来就要跑的那一个 Job,环只是在它之后
    # 追加批判与精化;把它算进来的话,一次正常开发烧掉份额的任务会在"零轮批判"处停住,
    # 而事件面只会说一句"预算用尽"——看上去像环坏了,其实是账算错了。
    spent = 0
    available = outcomes[0].tokens_available
    rounds = 0
    stopped = STOPPED_MAX_ROUNDS
    failed: tuple[str, ...] = ()

    while rounds < termination.max_rounds:
        if ceiling is not None and spent >= ceiling:
            stopped = STOPPED_BUDGET
            break
        verdict = await run_node(checker)
        outcomes.append(verdict)
        if verdict.suspended:
            return TopologyRun(outcomes=tuple(outcomes), stopped_because=STOPPED_SUSPENDED)
        spent += verdict.tokens_used
        available = available and verdict.tokens_available
        rounds += 1

        if not verdict.ok or verdict.payload is None:
            # 保留生成的代码现场，但 checker 没交出有效结论时本轮不能继续过门禁。
            # 失败节点交给外层的修复轮次处理，已完成的 work 节点仍可被诊断和复用。
            stopped = STOPPED_CHECKER_FAILED
            failed = (checker.id,)
            break
        if _converged(verdict, termination.stop_when):
            stopped = STOPPED_CONVERGED
            break
        if rounds >= termination.max_rounds:
            stopped = STOPPED_MAX_ROUNDS
            break
        if ceiling is not None and spent >= ceiling:
            stopped = STOPPED_BUDGET
            break

        improved = await run_node(refine)
        outcomes.append(improved)
        if improved.suspended:
            return TopologyRun(outcomes=tuple(outcomes), stopped_because=STOPPED_SUSPENDED)
        spent += improved.tokens_used
        available = available and improved.tokens_available

    return TopologyRun(
        outcomes=tuple(outcomes),
        stopped_because=stopped,
        rounds=rounds,
        tokens_used=spent,
        tokens_available=available,
        failed=failed,
    )


def _over_share(outcome: NodeOutcome, limits: ExecutionLimits) -> bool:
    """这个节点有没有花超它自己声明的份额。

    拿不到用量时**不判超**:不可得与 0 是两回事,而按"不可得 = 花光了"处理会让一个用量
    报不出来的运行时上的每个节点都失败。
    """
    share = outcome.node.budget_share
    if limits.budget_tokens is None or not outcome.tokens_available or share <= 0 or share >= 1:
        return False
    return outcome.tokens_used > int(limits.budget_tokens * share)


def _ceiling(termination: Termination, limits: ExecutionLimits) -> int | None:
    """环级预算的绝对上限。没配份额、或者不知道任务预算时,这条判据不生效。

    **轮次封顶管得住次数,管不住单轮的花费。** 两轮批判加两轮精化很可能是开发本身成本的
    两三倍,没有这条,loop engineering 就是 loop burning。
    """
    if termination.budget_share <= 0 or limits.budget_tokens is None:
        return None
    return int(limits.budget_tokens * termination.budget_share)


async def _run_assisted(
    template: TopologyTemplate, run_node: NodeRunner, limits: ExecutionLimits
) -> TopologyRun:
    """`assisted`:机器干一遍,人确认一次。

    **它不是环**:确认拒绝之后回到开发态重来一轮,而"重来一轮"是任务级的事(修复轮次),
    不是这张图内部的事。图内部再来一轮的话,轮次上限会有两套计数,而其中一套没人看得见。
    """
    if len(template.nodes) != 2 or template.nodes[1].kind is not NodeKind.CHECKER:
        raise UnknownTopology(
            f"{ASSISTED} 模板必须是一个工作节点加一个确认节点(checker): {template.id}"
        )
    work, confirm = template.nodes

    done = await run_node(work)
    if done.suspended or not done.ok or done.payload is None:
        # 机器那一步没成:确认无从谈起——没有产出可确认。
        return TopologyRun(
            outcomes=(done,),
            stopped_because=STOPPED_SUSPENDED if done.suspended else STOPPED_CHECKER_FAILED,
            tokens_used=done.tokens_used,
            tokens_available=done.tokens_available,
        )

    verdict = await run_node(confirm)
    if verdict.suspended:
        return TopologyRun(
            outcomes=(done, verdict),
            stopped_because=STOPPED_SUSPENDED,
            tokens_used=done.tokens_used + verdict.tokens_used,
            tokens_available=done.tokens_available and verdict.tokens_available,
        )
    if not verdict.ok or verdict.payload is None:
        return TopologyRun(
            outcomes=(done, verdict),
            stopped_because=STOPPED_CHECKER_FAILED,
            rounds=1,
            tokens_used=done.tokens_used + verdict.tokens_used,
            tokens_available=done.tokens_available and verdict.tokens_available,
            failed=(confirm.id,),
        )
    approved = not verdict.suspended and _converged(
        verdict, (template.termination or Termination()).stop_when
    )
    return TopologyRun(
        outcomes=(done, verdict),
        stopped_because=(
            STOPPED_CONVERGED if approved else STOPPED_REJECTED
        ),
        rounds=1,
        tokens_used=done.tokens_used + verdict.tokens_used,
        tokens_available=done.tokens_available and verdict.tokens_available,
    )


async def _run_dag(
    template: TopologyTemplate, run_node: NodeRunner, limits: ExecutionLimits
) -> TopologyRun:
    """`dag`:按拓扑序调度,无依赖的节点同时跑。

    三道筛子的**顺序与理由**从被取代的调度器继承:依赖满足(查集合)→ 不超并发上限(数在
    跑的)。写集不冲突那一道已经在**派发之前**由图校验器做掉了——它是纯数据判断,没必要留到
    调度那一刻再算一遍。

    **失败即冻结下游,旁支照跑。** 依赖失败节点的那些不派发:它们要读的产物不存在,派出去
    只是把钱花在一次必然失败上;而不依赖它的照跑——已经花的钱要产出价值,下一轮的诊断材料
    也越全越好。
    """
    if not template.nodes:
        raise UnknownTopology(f"{DAG} 模板至少要有一个节点: {template.id}")

    upstream: dict[str, set[str]] = {node.id: set() for node in template.nodes}
    for tail, head in template.edges:
        upstream[head].add(tail)

    done: dict[str, NodeOutcome] = {}
    failed: set[str] = set()
    frozen: set[str] = set()
    order: list[NodeOutcome] = []
    semaphore = asyncio.Semaphore(max(1, limits.max_parallel))

    async def guarded(node: TopologyNode) -> NodeOutcome:
        async with semaphore:
            return await run_node(node)

    while True:
        ready = [
            node
            for node in template.nodes
            if node.id not in done
            and node.id not in failed
            and node.id not in frozen
            and upstream[node.id] <= set(done)
        ]
        # 上游失败或被冻结的,自己也冻结:它要读的产物不存在。
        for node in template.nodes:
            if node.id in done or node.id in failed or node.id in frozen:
                continue
            if upstream[node.id] & (failed | frozen):
                frozen.add(node.id)
        if not ready:
            break
        batch = await asyncio.gather(*(guarded(node) for node in ready))
        for outcome in batch:
            order.append(outcome)
            over = _over_share(outcome, limits)
            if over:
                # **单节点超份额即这个节点失败,不拖垮全图。** 并行的钱不是新钱:同一个任务
                # 预算,花得更快也停得更准——一个节点把整份预算烧光的话,后面的节点会以
                # "预算不足"的名义失败,而真正的原因在别处。
                failed.add(outcome.node.id)
                continue
            if outcome.suspended:
                # 有节点派给了人:整张图停在这儿等他,已经跑完的不重跑(节点级幂等兜底)。
                return TopologyRun(
                    outcomes=tuple(order),
                    stopped_because=STOPPED_SUSPENDED,
                    tokens_used=sum(item.tokens_used for item in order),
                    tokens_available=all(item.tokens_available for item in order),
                )
            if outcome.ok and outcome.payload is not None:
                done[outcome.node.id] = outcome
            else:
                failed.add(outcome.node.id)

    return TopologyRun(
        outcomes=tuple(order),
        stopped_because=STOPPED_NODE_FAILED if failed else STOPPED_CONVERGED,
        rounds=1,
        tokens_used=sum(item.tokens_used for item in order),
        tokens_available=all(item.tokens_available for item in order),
        failed=tuple(sorted(failed)),
        frozen=tuple(sorted(frozen)),
    )


async def _run_best_of_n(
    template: TopologyTemplate, run_node: NodeRunner, limits: ExecutionLimits
) -> TopologyRun:
    """`best-of-n`:N 路变异,门禁筛选,judge 择优。

    **零过闸不是"选不出来",是全部失败**:回开发态并带上 N 路的失败对比——比单路失败报告
    信息量大得多,而这正是 N 倍成本另一半的辩护理由。
    """
    if len(template.nodes) != 3:
        raise UnknownTopology(f"{BEST_OF_N} 模板必须是尝试、门禁、裁决三个节点: {template.id}")
    attempt, gate, judge = template.nodes
    if not attempt.variants:
        raise UnknownTopology(f"{BEST_OF_N} 的尝试节点必须声明变体: {template.id}")

    outcomes: list[NodeOutcome] = []

    async def one(variant: Variant) -> tuple[str, NodeOutcome, NodeOutcome]:
        made = await run_node(replace(attempt, variants=(variant,)))
        judged = await run_node(replace(gate, variants=(variant,)))
        return variant.key, made, judged

    tried = await asyncio.gather(*(one(variant) for variant in attempt.variants))
    survivors: list[str] = []
    for key, made, judged in tried:
        outcomes.extend([made, judged])
        if made.suspended or judged.suspended:
            return TopologyRun(outcomes=tuple(outcomes), stopped_because=STOPPED_SUSPENDED)
        if judged.ok and (judged.payload or {}).get("passed"):
            survivors.append(key)

    if not survivors:
        return TopologyRun(
            outcomes=tuple(outcomes),
            stopped_because=STOPPED_NODE_FAILED,
            failed=tuple(variant.key for variant in attempt.variants),
            tokens_used=sum(item.tokens_used for item in outcomes),
            tokens_available=all(item.tokens_available for item in outcomes),
        )

    verdict = await run_node(judge)
    outcomes.append(verdict)
    picked = str((verdict.payload or {}).get("winner", ""))
    # **judge 只在过闸集合里选。** 它选了一个没过闸的就不算数——适应度先于偏好,
    # 而"它挑了个跑不起来的"该是一件系统自己能兜住的事,不是一次事故。
    winner = picked if picked in survivors else survivors[0]
    return TopologyRun(
        outcomes=tuple(outcomes),
        stopped_because=STOPPED_CONVERGED,
        rounds=1,
        winner=winner,
        survivors=tuple(survivors),
        tokens_used=sum(item.tokens_used for item in outcomes),
        tokens_available=all(item.tokens_available for item in outcomes),
    )


_EXECUTORS: dict[str, TopologyExecutor] = {
    BEST_OF_N: _run_best_of_n,
    DAG: _run_dag,
    SINGLE: _run_single,
    CRITIQUE_LOOP: _run_critique_loop,
    ASSISTED: _run_assisted,
    # 前置链:出题没出出来,这一轮不算。
    TEST_FIRST: _sequence_executor(verdict_ends_the_state=False),
    # 判定链:门禁没过、红队抓到东西,那本来就是这个状态要给出的答案。
    PROBE_AFTER_GATE: _sequence_executor(verdict_ends_the_state=True),
}


def register_executor(template_id: str, executor: TopologyExecutor) -> None:
    """挂一个执行器。后续模板(dag / critique-loop / best-of-n)从这里接进来。"""
    _EXECUTORS[template_id] = executor


def executor_for(template_id: str) -> TopologyExecutor:
    found = _EXECUTORS.get(template_id)
    if found is None:
        known = ", ".join(sorted(_EXECUTORS))
        raise UnknownTopology(f"没有这个模板的执行器: {template_id}(已注册: {known})")
    return found


async def run_topology(
    template: TopologyTemplate,
    run_node: NodeRunner,
    limits: ExecutionLimits | None = None,
) -> TopologyRun:
    """按模板跑一张图。"""
    return await executor_for(template.id)(template, run_node, limits or ExecutionLimits())


__all__ = [
    "ASSISTED",
    "ATTEMPT",
    "BEST_OF_N",
    "GATE",
    "JUDGE",
    "DAG",
    "CONFIRM",
    "CRITIQUE",
    "CRITIQUE_LOOP",
    "GENERATE",
    "REFINE",
    "IMPLEMENT",
    "PROBE",
    "PROBE_AFTER_GATE",
    "SINGLE",
    "UNIT_GATE",
    "TEST_FIRST",
    "WRITE_TESTS",
    "STOPPED_BUDGET",
    "STOPPED_CHECKER_FAILED",
    "STOPPED_CONVERGED",
    "STOPPED_MAX_ROUNDS",
    "STOPPED_NODE_FAILED",
    "STOPPED_REJECTED",
    "UNIT_GATE",
    "STOPPED_SUSPENDED",
    "ExecutionLimits",
    "Executor",
    "NodeKind",
    "NodeOutcome",
    "NodeRunner",
    "Termination",
    "TopologyNode",
    "TemplateChoice",
    "TopologyExecutor",
    "TopologyRun",
    "TopologyParseError",
    "TopologyRefused",
    "TopologyTemplate",
    "UnknownTopology",
    "Variant",
    "assisted_template",
    "best_of_n_template",
    "critique_loop_template",
    "dag_template",
    "probe_after_gate_template",
    "test_first_template",
    "executor_for",
    "parse_node",
    "parse_template",
    "parse_termination",
    "parse_variant",
    "loop_nodes",
    "register_executor",
    "run_topology",
    "single_template",
]
