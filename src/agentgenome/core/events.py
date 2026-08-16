"""任务事件流。

"这个任务为什么修了四轮?第三轮是哪个员工用哪一版能力干的?总共烧了多少 token?"——如果
这些信息只存在于滚动的终端输出里,出了事什么都查不到。这一层是审计与回放的最小事实集。

## 为什么双写

两个消费者的需求不同:**数据库**用来查询("这个任务的全部 gate 失败"),**JSONL** 用来流式
消费(前端 SSE 直接 tail 它)。写一份再导出另一份的话,导出那一步迟早会漏或会滞后。代价是
要证明两边对得上,所以那是这一层的重点测试。

## 数据库是真相来源,JSONL 是可清理的副本

两者双写不等于两者等价。**数据库永久保留**;JSONL 住在任务目录里、跟原始日志一起受保留期
管辖,超期会被 `security.retention` 清掉。这与 `core.task` 里"任务目录的 JSON 快照不是真相
来源"是同一条:两份会分叉的状态,分叉之后没有任何办法判断哪一份是对的,所以必须先说清楚谁
说了算。

一次操作可能被记在三个**记录平面**上,它们的寿命差了三个数量级:

| 平面 | 载体 | 回答的问题 | 寿命 |
|---|---|---|---|
| 事件面 | 这个模块(数据库 + JSONL 副本) | 什么时候发生了什么 | **永久**(以数据库为准) |
| 日志面 | `tasks/*/logs/` 的 Agent 输出流 | 员工具体怎么干的 | **有限**,见 `security.retention` |
| 版本面 | git(workspace 与子仓) | 资产变成了什么样 | **永久** |

把三者并排呈现而不写寿命,读起来像它们同样可靠——而日志面是唯一会消失的那个。

## 同一件事只由一个平面记内容,其他平面只记指针

**这是加一类记录时唯一需要回答的问题:内容归谁。** 没有一处权威说法的话,每次都会重新讨论
一遍、每次答案不同,而两种相反的错误都会发生:一件事哪里都没记(配置变更曾经就是),或者
两个地方各记一份、然后慢慢对不上——后者更难查,两份记录打架时没有任何办法判断哪份是对的。

按平面各自能记准的东西分:

| 这件事 | 内容归 | 其他平面记什么 |
|---|---|---|
| 配置变更 | 版本面(git diff) | 事件面记动作:真人身份、入口、结果 sha。**不记前后值** |
| 代码变更 | 版本面(commit) | 事件面记 Job 的起止与成本 |
| 知识/规则/工序变更 | 版本面(PR) | 事件面记 PR 引用与来源任务。**不复制知识内容** |
| 员工怎么干的 | 日志面(Agent 输出流) | 事件面记这次 Job 的结论与用量 |
| 状态迁移、审批、门禁结论 | **事件面**——它们本来就没有别的载体 | — |

判据是"哪个平面记得准":真人身份 git 记不下(提交用的是机器人身份),而配置的前值事件面
记不准(前端读到配置与人按下保存之间,别人可能已经改过一轮)。**两边都记的那一份,迟早会
在某个时刻不一致,而那时没有人能说清该信哪个。**

## 顺序靠自增序号,不靠时间戳

同一毫秒里写入的几条按时间戳排会乱序,而事件流乱序等于历史不可回放。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentgenome.core.store import SqliteStore

if TYPE_CHECKING:
    from agentgenome.core.genome_task import GenomeTask
    from agentgenome.core.genome_transitions import GenomeDecision, GenomeEvent
    from agentgenome.core.states import TaskEvent
    from agentgenome.core.task import Task
    from agentgenome.core.transitions import Decision

#: 事件流在任务目录里的位置。与 Job 日志同放 `logs/` 下——它们是同一类东西:
#: 逐条追加、给人 tail、不进 git。
EVENTS_FILE = Path("logs") / "events.jsonl"

#: 编排器自己作为行为主体时的 actor。
ORCHESTRATOR = "orchestrator"

#: 门禁作为行为主体时的 actor。它不是员工——没有 Agent 参与,零 token。
GATE_ACTOR = "gate-runner"

#: 集成入口作为行为主体时的 actor。**是常量不是散在各处的字符串**:靠字面量的话,推断
#: 类别的地方与写事件的地方各写一份,而改了其中一处之后老事件会悄悄换一个类别。
ALERT_ACTOR = "alert"
IM_ACTOR = "im"

#: 这些 actor 是集成入口。
INTEGRATION_ACTORS = frozenset({ALERT_ACTOR, IM_ACTOR})

#: 不属于任何任务的事件挂在这个保留 id 下。
#:
#: **用保留 id 而不是空值。** 空值需要每一处按任务查询各自记得排除,而漏掉的那一处会把
#: 系统级事件混进某个任务的时间线;保留 id 只需要在一处定义"这不是一个真任务"。
SYSTEM_SUBJECT = "system"


class ActorKind(StrEnum):
    """行为主体的类别。

    `actor` 本身是个自由字符串(人名、员工 id、保留名),按它筛选只能一个一个精确匹配;
    而审计员问的通常是**一整类**:"人干的有哪些"、"哪些是员工自己跑的"。没有这一层的话,
    这个问题要靠猜名字的形状来答,而猜出来的答案错了也不会有任何症状。
    """

    #: 真人。追责最终落到的那一类。
    HUMAN = "human"
    #: 数字员工。烧 token 的那一类。
    EMPLOYEE = "employee"
    #: 编排器自己。
    ORCHESTRATOR = "orchestrator"
    #: 门禁。没有 Agent 参与,零 token,所以不算员工。
    GATE = "gate"
    #: 集成入口(告警回调、群机器人之类)。既不是人也不是员工,把它算进任何一边都会让那一边
    #: 的统计失真。
    #:
    #: **取值不叫 `system`**:那与 `SYSTEM_SUBJECT` 撞了,而两者是完全不同的东西——一个说
    #: "这条不属于任何任务",一个说"这条是集成入口写的"。按 `actor_kind=system` 筛的人期待
    #: 的是前者,拿到的会是后者。
    INTEGRATION = "integration"


def infer_actor_kind(actor: str, kind: LogKind | None = None) -> ActorKind:
    """没有显式声明时,这个 actor 算哪一类。

    只有两个保留名认得出来,**其余一律算人**。这个方向是刻意选的:把一个人的操作记成机器,
    追责时那条线索直接断掉;反过来把一台机器记成人,只是让人多看一眼。

    员工猜不出来——员工 id 与人名在字符串上没有任何区别。所以写事件时**知道自己是员工的
    那几处必须显式传**(`job_finished` 与派发那一处),这也是为什么这个参数存在。

    `kind` 给得出来时会用上:Job 的起止事件只可能是员工写的,那是唯一一个能从事件本身反推
    出员工身份的地方——补老库时全靠它,不然升级之前的全部员工作业会一次性变成"人干的"。
    """
    if actor == ORCHESTRATOR:
        return ActorKind.ORCHESTRATOR
    if actor == GATE_ACTOR:
        return ActorKind.GATE
    if actor in INTEGRATION_ACTORS:
        return ActorKind.INTEGRATION
    if kind in _EMPLOYEE_KINDS:
        return ActorKind.EMPLOYEE
    return ActorKind.HUMAN


class LogKind(StrEnum):
    """事件类型。

    与 `agents.events.EventKind` 不是一回事:那个是 Agent 输出流的归一化事件(单次 Job 内部),
    这个是任务生命周期的事件(跨 Job)。刻意不同名。
    """

    TASK_CREATED = "task_created"
    #: 一个需求诞生了。subject 是需求 id(`req-` 前缀)——需求的动作记在需求名下,
    #: 任务事件里只带指针(`requirement_id`),内容不存两份。
    REQUIREMENT_CREATED = "requirement_created"
    #: 需求实体变了(改文本/优先级、搁置、恢复)。payload 的 `action` 说是哪一种;
    #: **只记动作不记内容**——当前文本在实体上,各次尝试读到的在任务快照里。
    REQUIREMENT_CHANGED = "requirement_changed"
    #: 一个需求被拆成了一批子需求(PRD 48)。subject 是**母需求** id,payload 带子需求
    #: id 清单与提案任务 id。拆分是一个动作、一条事件——不给每个子需求另记一条
    #: `requirement_created`:内容在实体上,这里只记指针清单,一件事只由一处记录。
    REQUIREMENT_SPLIT = "requirement_split"
    TRANSITION = "transition"
    TRANSITION_REFUSED = "transition_refused"
    JOB_STARTED = "job_started"
    JOB_FINISHED = "job_finished"
    #: 一次门禁执行的结果。质量把关的历史要可查。
    GATE_RESULT = "gate_result"
    #: needs_itest 的判定结论与理由。「为什么这次没跑集成测试」靠它回答。
    ITEST_DECISION = "itest_decision"
    #: 一次人工审批或驳回。审批人、时间与意见都在 payload 里——责任归属靠它。
    APPROVAL = "approval"
    #: 一次经验蒸馏的结果。失败也记——「这次蒸馏挂了」必须能被看到。
    DISTILLED = "distilled"
    ESCALATED = "escalated"
    #: 人已经处置完一次异常介入。状态仍保留原终态，payload 记录处置意见。
    INTERVENTION_RESOLVED = "intervention_resolved"
    #: 一次被批准的扩权:模块、理由、第几轮。**与越权分成两类事件**——合并的话
    #: "这个项目的员工越权频率"这个指标会被合法申请污染,而那个比率正是判断这套机制
    #: 该不该回调的唯一依据。
    SCOPE_WIDENED = "scope_widened"
    #: 一次配置变更。**只记动作,不记内容**——内容以版本面为准,见 `server.settings`。
    CONFIG_CHANGED = "config_changed"
    #: 工作区(项目)生命周期:创建、注册、注销。payload 的 `action` 说是哪一种。
    #: 事件记进**受影响工作区自己的事件面**——注销只摘注册表不动磁盘,这条记录因此留得住。
    WORKSPACE_CHANGED = "workspace_changed"
    #: 一次记录平面的缺口检测。**"查过没查过"本身也要有记录**,见 `security.gaps`。
    GAP_SCAN = "gap_scan"
    #: 一次会话的生命周期变化(创建/挂起/恢复/结束)。**三平面对会话没有例外**——
    #: 「谁在什么时候和哪个员工聊过」要查得到,而对话内容在日志面。
    SESSION = "session"
    #: 一次基因组资产的变更提案。**只记 PR 引用与来源任务,不记内容**——内容在版本面的
    #: 那个 PR 里。
    #:
    #: 目前只有规则会走到这里:经验卡片是直接落盘的,没有 PR 可指,它的可追溯性由
    #: `DISTILLED` 那条(挂在源任务上)承担。**写出来是因为"报告里没有"与"那里没事发生"
    #: 分不开**——下一个人看到这个类型只有一个产出者时,该知道那是已知的,不是漏了。
    GENOME_PR = "genome_pr"
    #: 一次战果转正提案:红队打穿过的攻击用例值不值得进业务仓的回归集。
    #:
    #: **与 `GENOME_PR` 分开**:那一条指向一个已经开出来的 PR,而这一条什么都还没提交——
    #: 它落地要走一次普通任务的正常提交路径。合并的话,"提过案"会被读成"已经进仓了",
    #: 而那两件事之间隔着门禁与评审。
    PROMOTION = "promotion"
    #: 一次员工供应:把一个数字员工对齐成了容器平台上的一个 Worker。
    #:
    #: **不复用 `CONFIG_CHANGED`**:那一条说的是"有人改了员工定义"(内容在版本面),
    #: 这一条说的是"有人把这个员工推上了平台"。两者的下一步完全不同——一个去看 git,
    #: 一个去看平台上那个容器。合并的话,"编制变了"与"定义变了"就再也分不开。
    #:
    #: 载荷只记动作与结果(员工、Worker 名、房间),**不记令牌**。
    WORKER_PROVISIONED = "worker_provisioned"
    #: 一次状态内部的执行拓扑实例:跑的是哪张图、有哪些节点与边、谁执行的。
    #:
    #: **图是数据不是黑箱**——前端画图的数据源就是它,不另造一份。写成日志散文的话,
    #: "这次到底跑了哪张图"只能靠读日志猜,而并行执行器出问题时那正是第一个要问的问题。
    TOPOLOGY = "topology"
    #: 一张派给人的待办的生命周期(投递/提醒/改派/提交/打回/升级)。
    #:
    #: **与审批分成两类事件**:审批答"这个产出可不可以",待办答"这份活谁来干"。合并的话,
    #: "人参与的比例"这个指标会把裁决与执行算成一件事,而自动化率恰恰要把它们分开数。
    TODO = "todo"
    NOTE = "note"


#: 只可能由员工写的事件类型。补老库时唯一能从事件本身反推出员工身份的线索。
_EMPLOYEE_KINDS = frozenset({LogKind.JOB_STARTED, LogKind.JOB_FINISHED})


@dataclass(frozen=True)
class Event:
    task_id: str
    ts: datetime
    actor: str
    kind: LogKind
    payload: dict[str, Any]
    #: 这个 actor 属于哪一类。见 `ActorKind`。
    actor_kind: ActorKind = ActorKind.HUMAN

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "ts": self.ts.isoformat(),
            "actor": self.actor,
            "actor_kind": self.actor_kind.value,
            "kind": self.kind.value,
            "payload": self.payload,
        }


_SCHEMA = """
create table if not exists events (
    seq integer primary key autoincrement,
    task_id text not null,
    ts text not null,
    actor text not null,
    actor_kind text,
    kind text not null,
    payload_json text not null
);
create index if not exists events_by_task on events (task_id, seq);
"""


class EventLog(SqliteStore):
    """任务事件的双写日志。"""

    schema = _SCHEMA
    added_columns = (("events", "actor_kind", "actor_kind text"),)

    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self._backfill_actor_kind()

    def _backfill_actor_kind(self) -> None:
        """老库里的行补上主体类别。

        **不能靠列的默认值。** 给它一个 `default 'human'` 的话,编排器、门禁与全部历史员工
        作业会一次性变成"人干的"——而按类别筛选恰恰是这一列存在的理由,于是这个维度从上线
        第一天起就是错的。按 actor 与事件类型反推一次,用的是新行同一个推断函数。

        **先只读地探一下有没有要补的。** 这个构造在每一次请求里都会跑到(不少是纯读路径),
        而 `update` 会拿写锁:无条件执行的话,一个跑着的服务端每读一次审计都要和编排器抢一次
        锁,症状是偶发的 `database is locked`——而它只在有负载时出现。

        JSONL 那份副本**不回填**。它是可清理的副本,真相在数据库(见模块说明);去重写历史
        日志文件既改不动已经归档的那些包,又会让"这一行是当时写的"这句话不再成立。
        """
        with self._connect() as connection:
            pending = connection.execute(
                "select seq, actor, kind from events where actor_kind is null"
            ).fetchall()
            for seq, actor, kind in pending:
                connection.execute(
                    "update events set actor_kind = ? where seq = ?",
                    (infer_actor_kind(actor, _log_kind(kind)).value, seq),
                )

    def jsonl_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / EVENTS_FILE

    # --- 写 ------------------------------------------------------------------

    def append(
        self,
        task_id: str,
        actor: str,
        kind: LogKind,
        payload: dict[str, Any],
        now: datetime | None = None,
        actor_kind: ActorKind | None = None,
    ) -> Event:
        """记一条事件。

        `actor_kind` 不传时按 `infer_actor_kind` 推——**员工那几处必须显式传**,理由见
        那个函数。
        """
        event = Event(
            task_id=task_id,
            ts=(now or datetime.now(UTC)).astimezone(UTC),
            actor=actor,
            actor_kind=actor_kind if actor_kind is not None else infer_actor_kind(actor, kind),
            kind=kind,
            payload=payload,
        )
        with self._connect() as connection:
            connection.execute(
                "insert into events (task_id, ts, actor, actor_kind, kind, payload_json) "
                "values (?, ?, ?, ?, ?, ?)",
                (
                    event.task_id,
                    event.ts.isoformat(),
                    event.actor,
                    event.actor_kind.value,
                    event.kind.value,
                    json.dumps(event.payload, ensure_ascii=False),
                ),
            )
        self._append_line(event)
        return event

    def transition(self, task: Task, event: TaskEvent, decision: Decision) -> Event:
        """记一次状态迁移。**被拒的也记**——不记的话"它怎么没动"这个问题查无可查。"""
        payload: dict[str, Any] = {
            "from": task.state.value,
            "to": decision.to.value if decision.to else None,
            "event": event.value,
            "effects": [effect.value for effect in decision.effects],
            "fix_rounds_delta": decision.fix_rounds_delta,
            "reason": decision.reason,
        }
        kind = LogKind.TRANSITION if decision.allowed else LogKind.TRANSITION_REFUSED
        return self.append(task.id, actor=ORCHESTRATOR, kind=kind, payload=payload)

    def genome_transition(
        self,
        task: GenomeTask,
        event: GenomeEvent,
        decision: GenomeDecision,
        actor: str = ORCHESTRATOR,
    ) -> Event:
        """记一次基因组任务的状态迁移。**被拒的也记**——与研发任务同一条:不记的话
        "它怎么没动"这个问题查无可查。

        两类任务写进**同一条流**,所以这里刻意不另起一套事件类型:审计问的是"什么时候发生
        了什么",而不是"这条属于哪一类任务"。
        """
        payload: dict[str, Any] = {
            "from": task.state.value,
            "to": decision.to.value if decision.to else None,
            "event": event.value,
            "effects": [effect.value for effect in decision.effects],
            "reason": decision.reason,
        }
        kind = LogKind.TRANSITION if decision.allowed else LogKind.TRANSITION_REFUSED
        # `actor` 缺省是编排器,但**人取消一个跑飞的任务、或者回答闸门时要记那个人**——
        # 记成编排器的话,"这个人干过什么"对这两种介入答不上来,而它们恰恰是人介入的全部。
        return self.append(task.id, actor=actor, kind=kind, payload=payload)

    def job_finished(
        self,
        task_id: str,
        employee_id: str,
        procedure_ref: str,
        ok: bool,
        tokens_used: int,
        tokens_available: bool,
        duration_s: float,
        failure_kind: str | None = None,
        failure_detail: str | None = None,
    ) -> Event:
        """记一次 Job 的结束。成本与责任都在这一条里。"""
        return self.append(
            task_id,
            actor=employee_id,
            actor_kind=ActorKind.EMPLOYEE,
            kind=LogKind.JOB_FINISHED,
            payload={
                "procedure_ref": procedure_ref,
                "ok": ok,
                # 拿不到用量时显式标记,**不填 0**:填 0 会让成本看板悄悄少算。
                "tokens_used": tokens_used if tokens_available else None,
                "tokens_available": tokens_available,
                "duration_s": duration_s,
                "failure_kind": failure_kind,
                "failure_detail": failure_detail,
            },
        )

    # --- 读 ------------------------------------------------------------------

    def events(self, task_id: str) -> tuple[Event, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "select task_id, ts, actor, kind, payload_json, actor_kind from events "
                "where task_id = ? order by seq asc",
                (task_id,),
            ).fetchall()
        return tuple(_row_to_event(row) for row in rows)

    def all_events(
        self,
        task_id: str | None = None,
        actor: str | None = None,
        kind: LogKind | None = None,
        kinds: tuple[LogKind, ...] = (),
        actor_kind: ActorKind | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> tuple[Event, ...]:
        """按各个维度查事件。审计检索、成本看板、Procedure 调用统计共用这一个入口。

        **限定任务也走这里**,而不是取回一个任务的全部事件再在 Python 里过一遍:两套过滤
        逻辑意味着加一个检索维度要改两处,而漏掉的那一处只会在"带了任务 id 的那种查法"下
        表现为"这个筛选条件不起作用"。

        **按 `seq` 降序**——审计员与看板都是"最近发生了什么"优先,不是"最早发生了什么"。
        `events()` 是另一回事:那是一条时间线,按发生顺序读才连贯。
        """
        clauses = []
        params: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if actor is not None:
            clauses.append("actor = ?")
            params.append(actor)
        if actor_kind is not None:
            clauses.append("actor_kind = ?")
            params.append(actor_kind.value)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if kinds:
            # **一次查几类,而不是取回来再在调用方过一遍。** 后者会在"这一页里恰好没有这几类"
            # 时给出一个自信的空结果,而它们只是排在了这一页之后。
            clauses.append(f"kind in ({', '.join('?' * len(kinds))})")
            params.extend(item.value for item in kinds)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("ts < ?")
            params.append(until.isoformat())
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"select task_id, ts, actor, kind, payload_json, actor_kind from events "
                f"{where} order by seq desc limit ?",
                (*params, limit),
            ).fetchall()
        return tuple(_row_to_event(row) for row in rows)

    def payload_values(self, kind: LogKind, field: str) -> frozenset[str]:
        """某一类事件里某个载荷字段的全部取值。

        **刻意不设条数上限。** 它服务于平面之间的比对(见 `security.gaps`),而比对少读一条
        就会把一次有记录的操作报成缺口——一份混着假缺口的报告,与没有报告等价。

        空值不进结果:比对的另一头是 sha,而空字符串不是任何一个提交。
        """
        with self._connect() as connection:
            rows = connection.execute(
                "select payload_json from events where kind = ?", (kind.value,)
            ).fetchall()
        found = {str(json.loads(row[0]).get(field, "")) for row in rows}
        return frozenset(found - {""})

    def tokens_used(self, task_id: str) -> int:
        """从事件流算出来的总用量。

        不直接信任务表上的那个数:事件流是逐条落盘的事实,任务表上的累加值是派生结果,
        两者对不上时该信前者。
        """
        return sum(
            event.payload["tokens_used"] or 0
            for event in self.events(task_id)
            if event.kind is LogKind.JOB_FINISHED
        )

    # --- 内部 ----------------------------------------------------------------

    def _append_line(self, event: Event) -> None:
        target = self.jsonl_path(event.task_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")
            # 任务跑到一半要能 tail,不是等结束才落盘。
            sink.flush()


def _log_kind(value: str) -> LogKind | None:
    """认不出来的事件类型当没有。回填一条老记录时炸掉的话,整个库就再也打不开了。"""
    try:
        return LogKind(value)
    except ValueError:
        return None


def _row_to_event(row: tuple[Any, ...]) -> Event:
    """一行 → 一个事件。

    两个查询各拼一次的话,加一列时改一处漏一处——而漏掉的那一处只会在某个具体查询上表现为
    "这个字段总是默认值"。
    """
    return Event(
        task_id=row[0],
        ts=datetime.fromisoformat(row[1]),
        actor=row[2],
        kind=LogKind(row[3]),
        payload=json.loads(row[4]),
        actor_kind=ActorKind(row[5]) if row[5] else infer_actor_kind(row[2], LogKind(row[3])),
    )


__all__ = [
    "ALERT_ACTOR",
    "EVENTS_FILE",
    "GATE_ACTOR",
    "IM_ACTOR",
    "INTEGRATION_ACTORS",
    "ORCHESTRATOR",
    "SYSTEM_SUBJECT",
    "ActorKind",
    "Event",
    "EventLog",
    "LogKind",
    "infer_actor_kind",
]
