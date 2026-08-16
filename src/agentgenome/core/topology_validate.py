"""图校验器:派发前把一张图判死。

## 为什么它必须独立于执行器

"派发前全绿"这条承诺,只有在校验器不需要任何执行器就能跑的时候才成立。所以这里是**纯函数**
——不碰数据库、不碰工作区、不派发任何东西,输入一张图,输出一串可操作的问题。

## 为什么这些检验值得做

图工作流最常见的两种毛病,在提示词级的编排里根本不可判定,而在我们这里是**机器可判定**的:

- **假边**:LLM 拆图时会顺手写下"先 A 后 B"这种时序直觉。但等待一个不给你任何东西的上游
  是纯浪费——边只有在真实的产物流动上才成立,这是「文件即协议」在图上的直接推论。
- **写集相交**:代码任务的独立性不只是数据流独立,还要写集不相交。因为员工的可写路径本来
  就是声明出来的,这条在我们这里可判定,而在别处只能等合并时炸。

## 报错的口径

逐条、指名到具体的边 / glob / 环路径。这些报错的第一读者是下一次拆图的 LLM,
"节点 B 声明消费 spec.md,但无上游产出它"能直接改,"图不合法"不能。
"""

from __future__ import annotations

from dataclasses import dataclass

from agentgenome.core.topology import FIELD_NAME, NodeKind, TopologyTemplate

#: 结构问题:边指向不存在的节点。**先报结构再报规则**——拿着一个不存在的节点去查产物背书,
#: 只会报出一个误导性的第二问题。
UNKNOWN_NODE = "unknown-node"
#: 结构问题:节点 id 重复。id 是产物编址的一部分,重复等于两个节点共用一个产物目录。
DUPLICATE_NODE = "duplicate-node"
#: 边无产物背书。
FAKE_EDGE = "fake-edge"
#: 并行节点写集相交。
WRITE_CONFLICT = "write-conflict"
#: 依赖成环。
CYCLE = "cycle"
#: checker 声明了写集。
CHECKER_WRITES = "checker-writes"
#: 终止判据本身不合法。
BAD_TERMINATION = "bad-termination"


@dataclass(frozen=True)
class TopologyIssue:
    """一条可操作的校验问题。"""

    code: str
    message: str
    #: 指名到谁:节点 id、"a→b"、或冲突的 glob 对。
    where: str = ""

    def render(self) -> str:
        where = f" [{self.where}]" if self.where else ""
        return f"{self.code}{where}: {self.message}"


def validate_topology(template: TopologyTemplate) -> tuple[TopologyIssue, ...]:
    """跑全部检验。全绿返回空元组。"""
    structural = _structure(template)
    if structural:
        # 结构不成立时就此打住:后面的规则全部建立在"边两端都存在"之上。
        return structural
    return (
        *_fake_edges(template),
        *_write_conflicts(template),
        *_cycles(template),
        *_checker_write_scopes(template),
        *_termination(template),
    )


def _structure(template: TopologyTemplate) -> tuple[TopologyIssue, ...]:
    issues: list[TopologyIssue] = []
    seen: set[str] = set()
    for node in template.nodes:
        if node.id in seen:
            issues.append(
                TopologyIssue(
                    DUPLICATE_NODE,
                    "节点 id 重复。id 进产物编址,重复等于两个节点共用一个产物目录。",
                    node.id,
                )
            )
        seen.add(node.id)
    for upstream, downstream in template.edges:
        for endpoint in (upstream, downstream):
            if endpoint not in seen:
                issues.append(
                    TopologyIssue(
                        UNKNOWN_NODE,
                        f"边引用了不存在的节点 {endpoint}。",
                        f"{upstream}→{downstream}",
                    )
                )
    return tuple(issues)


def _fake_edges(template: TopologyTemplate) -> tuple[TopologyIssue, ...]:
    """边合法 ⟺ 下游消费了上游的某个产物。

    **拒绝,不是静默剪掉。** 自动剪边会让"事件面可回放"回放一张没人写过的图。
    """
    issues = []
    for upstream, downstream in template.edges:
        producer = template.node(upstream)
        consumer = template.node(downstream)
        if not set(consumer.needs) & set(producer.produces):
            wanted = ", ".join(consumer.needs) or "(什么都不消费)"
            issues.append(
                TopologyIssue(
                    FAKE_EDGE,
                    f"节点 {downstream} 声明消费 {wanted},但上游 {upstream} 不产出其中任何一个"
                    f"(它产出 {', '.join(producer.produces) or '(什么都不产出)'})。"
                    "先后顺序不构成边,只有真实的产物流动构成边。",
                    f"{upstream}→{downstream}",
                )
            )
    return tuple(issues)


def _write_conflicts(template: TopologyTemplate) -> tuple[TopologyIssue, ...]:
    """两个**无依赖路径**的节点写集相交 → 拒绝。

    谓词要写准:节点各自独立工作树时,相交在执行期不是错误,它预判的是**拓扑序合并回任务
    分支时的冲突**。有依赖就不并行,不并行就不会撞——规则管的是并行,不是写集本身。

    **它的粗粒度先祖是"同仓串行"**(来自被取代的子任务图):两个分支同时改同一个业务仓,
    第二个的 rebase 会把第一个的改动卷进来,然后越权检查把它判成"动了不该动的东西"——
    而实际上谁都没越权。那条规则按**仓**判,这条按**声明的写集**判,更细但同一件事;
    子模块指针的推进仍然按仓串行,细化不等于取消。

    **同一节点的不同 variant 天然豁免**:规则比较的是两个节点,而变体属于同一个节点。
    变体按设计写同一批路径(它们是同一件事的不同做法),靠分支物理隔离,且只有一路会被合并。
    """
    reachable = _closure(template)
    issues = []
    nodes = list(template.nodes)
    for index, left in enumerate(nodes):
        for right in nodes[index + 1 :]:
            if right.id in reachable[left.id] or left.id in reachable[right.id]:
                continue
            for mine in left.write_scope:
                for theirs in right.write_scope:
                    if globs_overlap(mine, theirs):
                        issues.append(
                            TopologyIssue(
                                WRITE_CONFLICT,
                                f"并行节点 {left.id} 与 {right.id} 的写集相交"
                                f"({mine} ∩ {theirs}):拓扑序合并回任务分支时会冲突。"
                                "要么拆开写集,要么用一条有产物背书的边把它们串起来。",
                                f"{left.id}↔{right.id}",
                            )
                        )
    return tuple(issues)


def _cycles(template: TopologyTemplate) -> tuple[TopologyIssue, ...]:
    """依赖图必须无环。

    **在加载那一刻拒绝,不在调度那一刻**(这条判断从旧的子任务图继承)。调度时才发现的
    症状是"任务提交之后什么都没发生"的静默死锁,没有任何错误信息,排查极贵。

    反馈环不在此列:它由 `Termination` 表达,不是一条依赖边。
    """
    outgoing = _adjacency(template)
    visiting: list[str] = []
    done: set[str] = set()
    found: list[TopologyIssue] = []

    def walk(node_id: str) -> None:
        if found or node_id in done:
            return
        if node_id in visiting:
            path = visiting[visiting.index(node_id) :] + [node_id]
            found.append(
                TopologyIssue(
                    CYCLE,
                    f"依赖成环: {' → '.join(path)}。"
                    "反馈环属于 termination 机制,不属于依赖边。",
                    " → ".join(path),
                )
            )
            return
        visiting.append(node_id)
        for nxt in outgoing[node_id]:
            walk(nxt)
        visiting.pop()
        done.add(node_id)

    for node in template.nodes:
        walk(node.id)
    return tuple(found)


def _checker_write_scopes(template: TopologyTemplate) -> tuple[TopologyIssue, ...]:
    """checker 产出判定,不产出资产。

    它进产物面与事件面,不进版本面——一个能改代码的批判者会不自觉地把批判改成迁就。
    """
    return tuple(
        TopologyIssue(
            CHECKER_WRITES,
            f"检查节点 {node.id} 声明了写集 {', '.join(node.write_scope)}。"
            "checker 产出判定而不是资产,不该碰版本面。",
            node.id,
        )
        for node in template.nodes
        if node.kind is NodeKind.CHECKER and node.write_scope
    )


def _adjacency(template: TopologyTemplate) -> dict[str, list[str]]:
    """节点 → 它的下游。环检测与可达性是同一张邻接表的两种走法,建两遍的话它们会分叉。"""
    outgoing: dict[str, list[str]] = {node.id: [] for node in template.nodes}
    for upstream, downstream in template.edges:
        outgoing[upstream].append(downstream)
    return outgoing


def _termination(template: TopologyTemplate) -> tuple[TopologyIssue, ...]:
    """终止判据自己得先成立。

    **在加载那一刻拒绝,不在跑到一半时**(ADR-0008)。一个拼错的收敛谓词若等到执行期才炸,
    生成与第一轮批判的钱已经花掉了,而报错说的是"谓词不对"——那时最该问的问题是"为什么这
    张图被派出去了"。
    """
    termination = template.termination
    if termination is None:
        return ()
    issues = []
    if termination.max_rounds < 1:
        issues.append(
            TopologyIssue(
                BAD_TERMINATION,
                f"max_rounds 必须 ≥ 1,收到 {termination.max_rounds}。"
                "环一轮都不跑的话,它与没有这张图没有区别。",
                template.id,
            )
        )
    if termination.stop_when is not None and not FIELD_NAME.match(termination.stop_when):
        issues.append(
            TopologyIssue(
                BAD_TERMINATION,
                f"stop_when 只支持产物里的一个布尔字段名,收到 {termination.stop_when!r}。"
                "表达式不支持:一个只有内置模板用得上的小语言不值得发明。",
                template.id,
            )
        )
    if not 0 <= termination.budget_share <= 1:
        issues.append(
            TopologyIssue(
                BAD_TERMINATION,
                f"budget_share 是任务预算的份额,应落在 [0, 1],收到 {termination.budget_share}。",
                template.id,
            )
        )
    return tuple(issues)


def _closure(template: TopologyTemplate) -> dict[str, set[str]]:
    """每个节点能到达谁。图很小,直接跑传递闭包。"""
    outgoing = _adjacency(template)
    closure: dict[str, set[str]] = {}
    for node in template.nodes:
        seen: set[str] = set()
        stack = list(outgoing[node.id])
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(outgoing.get(current, ()))
        closure[node.id] = seen
    return closure


#: 判不准的字符。**碰到就判相交**——保守方向是这条规则的全部价值:少并行的代价是慢,
#: 放过一次相交的代价是两个节点静默互相覆盖,而后者没有任何症状指向这里。
_UNDECIDABLE = ("[", "]", "{", "}")


def globs_overlap(left: str, right: str) -> bool:
    """两个 glob 有没有可能同时匹配到同一个路径。

    `**` 跨目录,`*` 与 `?` 不跨分隔符。这是两个模式语言的交集非空判定——对纯 `*`/`?`
    的模式是精确的,遇到字符类一律保守地判相交。
    """
    if any(token in left or token in right for token in _UNDECIDABLE):
        return True
    return _segments_overlap(_split(left), _split(right))


def _split(pattern: str) -> tuple[str, ...]:
    cleaned = pattern.strip().lstrip("./")
    return tuple(part for part in cleaned.split("/") if part)


def _segments_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left and not right:
        return True
    if not left:
        return all(part == "**" for part in right)
    if not right:
        return all(part == "**" for part in left)
    if left[0] == "**":
        return _segments_overlap(left[1:], right) or _segments_overlap(left, right[1:])
    if right[0] == "**":
        return _segments_overlap(left, right[1:]) or _segments_overlap(left[1:], right)
    return _one_segment_overlaps(left[0], right[0]) and _segments_overlap(left[1:], right[1:])


def _one_segment_overlaps(left: str, right: str) -> bool:
    """单个路径段上两个 `*`/`?` 模式的交集非空判定。"""
    memo: dict[tuple[int, int], bool] = {}

    def walk(i: int, j: int) -> bool:
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == len(left) and j == len(right):
            result = True
        elif i == len(left):
            result = all(char == "*" for char in right[j:])
        elif j == len(right):
            result = all(char == "*" for char in left[i:])
        elif left[i] == "*":
            result = walk(i + 1, j) or walk(i, j + 1)
        elif right[j] == "*":
            result = walk(i, j + 1) or walk(i + 1, j)
        elif left[i] == "?" or right[j] == "?":
            result = walk(i + 1, j + 1)
        else:
            result = left[i] == right[j] and walk(i + 1, j + 1)
        memo[key] = result
        return result

    return walk(0, 0)


__all__ = [
    "BAD_TERMINATION",
    "CHECKER_WRITES",
    "CYCLE",
    "DUPLICATE_NODE",
    "FAKE_EDGE",
    "UNKNOWN_NODE",
    "WRITE_CONFLICT",
    "TopologyIssue",
    "globs_overlap",
    "validate_topology",
]
