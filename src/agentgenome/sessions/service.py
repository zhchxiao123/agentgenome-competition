"""会话服务:创建、发消息、治理。

## 一条线不能过

**对话能产生认知、草稿与结论,不能绕过门禁、安全检查与审批把变更合进主线。** 这个模块里
每一处看起来可以走捷径的地方,都是这条线的具体形态。

## workdir 由「能不能写 × 关不关任务」推出

对 `claude 2.1.220` 的实测:`--resume` 按 **cwd 路径**分桶存会话,换一个 cwd 续接直接失败;
但没有短期 TTL,目录删掉再按同路径重建仍可续接。**所以工作目录必须在创建时一次定死**——
不存在"先在别处聊、要写文件时再切进去"这种懒切换,那会让第二条消息续接失败,而症状
("聊了两句它就不记得了")与运行时挑错时一模一样,极难归因。

于是:

- **只读**:项目根 checkout。只读工具集拿不到写工具,在根上跑不出副作用;而根不会被清理,
  会话寿命天然独立于任何任务。早先这里用的是 `~/.agentgenome/sessions/<id>/` 一个空目录,
  理由是"躲开 worktree 的生命周期"——躲对了,但躲进了一个**看不见代码**的地方:上下文包
  里不含路径、命令行里没有 `--add-dir`、工具集里没有 Bash,员工的 Grep 搜的是一个空文件夹,
  而开场白还告诉它"基于项目的代码回答"。项目根同时满足这两条,那个空目录没有存在理由。
- **可写 + 有任务**:任务 worktree,**其会话寿命因此等于任务**。这是正确语义而非缺陷。
- **可写 + 无任务**:会话自己的 worktree(分支 `session/<id>`)。改动要有地方落,而那个
  地方不能是主线的工作树——出口是转成任务,不是直接进主线。

## 只读工具集在这一层写死

不从 `EmployeeConfig` 继承。员工定义里的 allow 是为写代码准备的,继承过来就等于只读会话
名不副实——而"只读"必须是拿不到写工具,不是靠员工自觉。**只读会话跑在项目根上,这一条
因此从"洁癖"变成了"唯一的防线"。**
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from agentgenome import paths
from agentgenome.agents.capabilities import capabilities_of
from agentgenome.agents.runtime import SessionCapableRuntime, SessionHandle, SessionSpec
from agentgenome.core.events import ORCHESTRATOR, EventLog, LogKind
from agentgenome.employees import EmployeeConfig, load_employees, workspace_employees_root
from agentgenome.genome import craft
from agentgenome.sessions import log
from agentgenome.sessions.blocks import Block, BlockKind, blocks_from
from agentgenome.sessions.model import (
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_MAX_TOKENS,
    Session,
    SessionState,
    SuspendReason,
)
from agentgenome.sessions.store import SessionStore
from agentgenome.space.git_ws import GitWorkspace

#: 开场指令的两段。**放在这里而不是 prompt.md**:会话不是工序,它没有产物契约,借用工序的
#: prompt 会让"这次该产出什么"这个问题变得含糊。
#:
#: 拆成"权限"与"任务"两段拼接,而不是按模式各写一整段:两个自由度正交,四种组合各写一份
#: 全文的话,同一句话会被抄四遍,而改其中一遍就是一次静默分叉。
_PROMPT_READ_ONLY = (
    "你在一次**只读会话**里。用户想问清楚一些事。\n"
    "你只能读,不能改任何文件。基于项目的知识与代码回答;不确定就说不确定,不要编。"
)
_PROMPT_WRITABLE = (
    "你在一次**可写会话**里。用户会边聊边指方向,你负责改代码。\n"
    "改动照样要过门禁与审批——**人参与不等于少走流程**。"
)
_PROMPT_WITH_TASK_READ_ONLY = (
    "\n\n这次会话关联了一个任务。先看下面列出的它的产物与日志,再回答;"
    "答案要能指到具体的证据。"
)
_PROMPT_WITH_TASK_WRITABLE = "\n\n你在这个任务的隔离工作区里改代码,改动落在它的分支上。"


def session_prompt(writable: bool, task_id: str | None) -> str:
    """这次会话的开场指令。两个自由度各出一段。"""
    head = _PROMPT_WRITABLE if writable else _PROMPT_READ_ONLY
    if not task_id:
        return head
    return head + (_PROMPT_WITH_TASK_WRITABLE if writable else _PROMPT_WITH_TASK_READ_ONLY)


class SessionRefused(RuntimeError):
    """这次操作不被允许。

    与 `SessionNotFound` 分开:调用方要能把"没有这个会话"与"有但不让你这么干"分开记录。
    """


class SessionsUnsupported(RuntimeError):
    """这个运行时开不了会话。

    在 `preflight` 阶段就该暴露,不是等用户创建会话时才失败——但如果真走到了创建这一步,
    也要明确报错而不是静默降级成一个永远不回话的会话。
    """


def workdir_for(
    root: Path, session_id: str, writable: bool, task_worktree: Path | None
) -> Path:
    """这个会话在哪儿跑。见模块文档——这不是一个随便填的字段。

    **纯计算,不建目录。** 建目录(尤其是 `git worktree add`)是有副作用的,放在这里会让
    "算一下它该在哪"这个问题没法单独回答——而测试与错误信息都需要能单独回答它。
    """
    if not writable:
        return Path(root)
    if task_worktree is not None:
        return task_worktree
    return GitWorkspace(root).session_worktree_path(session_id)


class RunningTurn:
    """一轮问答的后台执行体。**与任何一次 HTTP 连接解耦。**

    ## 为什么要单独绑一个任务

    `SessionService.send` 的账在它自己的 `finally` 里结,前提是它跑到底。以前"跑到底"
    依赖调用方一直在场——SSE 连接一断,`send()` 所在的协程被回收,`finally` 靠 GC 兜底,
    时机不可控,而"关页面"和"用户点了停止"因此被迫共用同一条退出路径。

    这里改成显式 `asyncio.create_task`:任务的生死不再由任何一次连接决定,`send()`
    永远正常跑完一次,账永远结一次。副作用正是产品要的那个——关掉页面不再打断生成,
    重新打开能接着看。真正的"停止"因此也必须变成一个显式动作(`SessionService.stop_turn`),
    不能再靠断连接顺带实现。

    ## 多个订阅者

    重开的页面、原来没关的标签页可以同时接着看同一轮——每个订阅者一个独立队列,一份产出
    广播给所有人。写法照抄 `server/bus.py` 的 `Subscription`:那边解决的是同一类问题
    (一份产出、多个可能随时掉线的看客),这里没有理由另起一套。
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.task: asyncio.Task[None] | None = None
        self._subscribers: list[asyncio.Queue[Block | None]] = []

    def subscribe(self) -> asyncio.Queue[Block | None]:
        queue: asyncio.Queue[Block | None] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Block | None]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def publish(self, block: Block) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(block)

    def finish(self) -> None:
        """这一轮结束了。**哨兵是 `None`**——订阅者靠它区分"还没到"和"不会再有了"。"""
        for queue in list(self._subscribers):
            queue.put_nowait(None)
        self._subscribers.clear()


class SessionService:
    """会话的唯一入口。

    与 `AgentPool` 是执行 Job 的唯一入口同一个理由:预算、超时、只读约束、三平面记录
    都在这里,绕过去等于让这四样失效。
    """

    def __init__(
        self,
        workspace_root: Path,
        store: SessionStore,
        runtimes: dict[str, object],
    ) -> None:
        self.root = Path(workspace_root)
        self.store = store
        self.runtimes = runtimes
        #: 事件面。**三平面对会话没有例外**——「谁在什么时候和哪个员工聊过」要查得到。
        self.log = EventLog(self.root)
        #: 已开会话的运行时句柄。进程内,重启后重新 `start_session`(廉价,不拉进程)。
        self._handles: dict[str, SessionHandle] = {}
        #: 正在后台跑的轮次。进程内、按 session 键一个——同一 session 同一时刻只能有一轮
        #: 在跑,见 `start_turn`。重启后这份是空的,和 `_handles` 同一条理由:跨重启的
        #: 续播不在这次的范围内,正在跑的那一轮随进程一起没了,已落盘的部分不会丢。
        self._turns: dict[str, RunningTurn] = {}

    # --- 创建 ----------------------------------------------------------------

    async def create(
        self,
        employee: EmployeeConfig,
        writable: bool = False,
        title: str = "",
        task_id: str | None = None,
        task_worktree: Path | None = None,
        context: str | None = None,
        runtime_name: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        now: datetime | None = None,
    ) -> Session:
        """开一个会话。

        `task_id` 是可选的,与 `writable` 正交——两个都不给是完全正常的一种(在项目根上
        只读地聊)。

        `max_tokens` 由调用方从项目配置(`budgets.session_tokens`)取,**这一层不读配置**:
        读了的话它就得知道自己的根下面一定有一份可加载的配置,而这个类在测试与 CLI 里都被
        直接构造过。缺省 0 = 不限。

        ## 能提前判掉的一律在落盘之前判完

        **会话表里的每一条都应当是可用的。** 早先运行时解析排在落盘之后,于是解析失败会
        留下一条 `active` 的僵尸:列表里看着健康,点进去永远得到"会话还没开起来",而且
        每失败一次多攒一条,故障期越长列表越脏。

        列表里躺着一条永远发不出消息的会话,比创建时直接报错坏得多——用户会反复点它,
        每次都得到同一句无从下手的错误。
        """
        name = runtime_name or employee.runtime
        profile = capabilities_of(name)
        if profile is not None and not profile.sessions:
            raise SessionsUnsupported(f"运行时 {name} 不支持会话")
        # 解析放在落盘**之前**,见上。拿到的句柄留到最后一步用。
        runtime = self._runtime(name)
        # 工作区占用也在落盘之前判,同一条理由。
        self._refuse_if_occupied(writable, task_id)

        session = self.store.create(
            employee_id=employee.id,
            runtime=name,
            writable=writable,
            title=title,
            task_id=task_id,
            max_tokens=max_tokens,
            idle_timeout_s=DEFAULT_IDLE_TIMEOUT_S,
            now=now,
        )
        workdir = self._prepare_workdir(session.id, writable, task_worktree)

        # 会话里的员工与自主执行的员工带着同一套手艺(PRD 26)。
        crafts = self._mount_crafts(
            workdir, craft.collect(None, craft.common_root(self.root), employee.crafts)
        )

        # 上下文包落盘,保证任意一轮可离线复现。**默认真的去组装一份**——早先这里是
        # 调用方不传就落一个空文件,于是会话带着空上下文上岗:员工看不到项目规则,也看不到
        # 任何知识卡片,而"它答得不好"完全无法归因到这里。
        log.session_dir(self.root, session.id).mkdir(parents=True, exist_ok=True)
        bundle, items = (
            (context, ())
            if context is not None
            else self._assemble(employee, session, title, crafts)
        )
        log.context_path(self.root, session.id, 1).write_text(bundle, encoding="utf-8")

        session = session.evolve(workdir=str(workdir), context_items=items)

        handle = await runtime.start_session(
            SessionSpec(
                session_id=session.id,
                employee_id=employee.id,
                workdir=workdir,
                context_file=log.context_path(self.root, session.id, 1),
                tools_allow=list(session.tools) or list(employee.tools.allow),
                tools_deny=list(employee.tools.deny) if session.writable else [],
                max_tokens=session.max_tokens,
            )
        )
        self._handles[session.id] = handle
        # **运行时给的标识必须落盘。** 它只留在内存句柄里的话,进程一重启就没有任何东西
        # 能把这个会话和运行时那一侧的会话对上——而字段和列一直都在,只是从没被写过。
        session = session.evolve(native_session_id=handle.native_session_id)
        self.store.save(session)
        self._record(session, "created")
        return session

    def _prepare_workdir(
        self, session_id: str, writable: bool, task_worktree: Path | None
    ) -> Path:
        """把 `workdir_for` 算出来的那个目录真的准备好。

        只读会话落在项目根上,它已经存在;可写且无任务的会话要现开一个 worktree。
        """
        workdir = workdir_for(self.root, session_id, writable, task_worktree)
        if writable and task_worktree is None:
            GitWorkspace(self.root).checkout_session(session_id)
        else:
            workdir.mkdir(parents=True, exist_ok=True)
        return workdir

    def _mount_crafts(
        self, workdir: Path, sources: dict[str, Path]
    ) -> tuple[tuple[str, str], ...]:
        """把手艺送到员工手边。返回要**内联**进上下文包的那些(物化成功则为空)。

        **项目根上不物化。** `craft.materialize` 会先 `rmtree` 掉目标下的 `.claude/skills`
        再复制——那个"先清空"对 worktree 是对的(它是一次性的派生视图,残留会让"这个员工
        只看得到自己的手艺"不成立),但项目根不是派生视图,**它是用户的仓库**。在那里执行
        同一套动作会把仓库自己的 `.claude/skills/` 删掉,而删掉的东西与会话毫无关系。

        降级路径直接借 Job 那一条(`genome/dispatch._mount_or_inline`):手艺全文内联进
        上下文包。代价是吃预算、没有渐进式加载,但"员工没带手艺"是最难归因的一类现象
        ——它没有任何报错,只表现为"最近它好像变笨了"。
        """
        if workdir != self.root:
            craft.materialize(workdir, sources)
            return ()
        bodies: list[tuple[str, str]] = []
        for name in sorted(sources):
            manifest = sources[name] / craft.CRAFT_MANIFEST
            with contextlib.suppress(OSError):
                bodies.append((name, manifest.read_text(encoding="utf-8")))
        return tuple(bodies)

    def _refuse_if_occupied(self, writable: bool, task_id: str | None) -> None:
        """同一个可写工作区上不允许开第二个可写会话。

        只读会话不参与:它们拿不到写工具,同处一个目录不会互相踩。

        没有这一条的话,两个可写会话会拿到**同一个 cwd**,两个 `claude` 进程同时改同一批
        文件——而 `start_turn` 那把锁只管一个会话内部的轮次,跨会话什么都不挡。以前这个洞
        被"结对会话通过界面根本建不出来"掩盖着(见 PRD 45 issue 01),它一直在,只是没人
        走到过。

        **报错要点名占用者。** 只说"工作区被占用"的话,用户不知道该去关掉哪一个。
        """
        if not writable or not task_id:
            return
        occupied = [
            item
            for item in self.store.list(writable=True, state=SessionState.ACTIVE)
            if item.task_id == task_id
        ]
        if occupied:
            ids = "、".join(item.id for item in occupied)
            raise SessionRefused(
                f"任务 {task_id} 的工作区已经被可写会话 {ids} 占着。"
                "两个会话同时改同一批文件会互相覆盖;先结束它,或者开一个只读会话。"
            )

    # --- 对话 ----------------------------------------------------------------

    async def ensure_open(self, session_id: str, now: datetime | None = None) -> Session:
        """这个会话现在能不能收消息。不能就抛,说清楚为什么。

        单拎出来是因为 **SSE 一旦开出去就没法改状态码了**:拒绝要发生在开流之前,
        否则前端只能看见一条 200 的空流,而"没权限"与"它没话说"分不开。**句柄重建也
        必须发生在这里**,理由相同——重建失败要变成一个状态码,不是一条空流。
        """
        session = self._enforce_idle(self.store.get(session_id), now or datetime.now(UTC))
        if not session.accepts_messages:
            raise SessionRefused(self._why_closed(session))
        if session_id not in self._handles:
            self._handles[session_id] = await self._rehydrate(session)
        return session

    async def _rehydrate(self, session: Session) -> SessionHandle:
        """进程重启之后,按库里那份把会话重新开起来。

        以前内存里没有句柄就直接抛"还没开起来",于是**每次重启都让全部历史会话变成僵尸**
        ——用户看到的是"它把我们聊过的都忘了",而列表里那些会话还在,更像产品在骗人。

        **走的是 `start_session` 而不是自己拼一个句柄。** 运行时那一侧也按会话缓存着规格
        (工具集、工作区、预算),那份缓存同样是进程内的;只把句柄装回去的话,下一条消息会
        在运行时里撞上"这个会话没开过"。`start_session` 按契约不拉起任何进程,重放它正是
        为这种情况准备的——把持久化的运行时标识传进去,它就不会另生成一个。

        **实测确认续接只认工作目录的路径字符串、不认目录内容,也没有短期 TTL**(见 PRD 27),
        所以这条路不需要任何额外机制。

        字段不齐时报的是**另一句话**:「重启后无法恢复」只能新开一个,「还没开起来」是等它
        开好,两者的下一步动作不同。合成一句等于让用户去等一件永远不会发生的事。
        """
        if not session.native_session_id or not session.workdir:
            raise SessionRefused(
                f"会话 {session.id} 在服务重启后无法恢复(缺少运行时标识或工作区)。新建一个吧。"
            )
        employee = load_employees(workspace_employees_root(self.root)).get(session.employee_id)
        handle = await self._runtime_for(session).start_session(
            SessionSpec(
                session_id=session.id,
                employee_id=session.employee_id,
                workdir=Path(session.workdir),
                context_file=log.context_path(self.root, session.id, 1),
                tools_allow=list(session.tools) or list(employee.tools.allow),
                tools_deny=list(employee.tools.deny) if session.writable else [],
                max_tokens=session.max_tokens,
                native_session_id=session.native_session_id,
            )
        )
        # 续接标记跟着回来,否则重建之后第一条消息又会当作"新建"发出去。
        handle.started = session.started
        return handle

    async def send(
        self, session_id: str, message: str, now: datetime | None = None
    ) -> AsyncIterator[Block]:
        """发一条消息,**逐块**产出(每块落盘之后才 yield)。**用户自己那句也在内**。

        ## 记账在 `finally`

        调用方可能中途退出(客户端断线、用户按停止)。那一刻 token 已经烧掉了,而
        `async for` 那一侧走不到收尾——所以 token 累加、预算挂起、`last_seq` 更新全部
        放在 `finally`,无论怎么退出都会结账。

        少了这一条的表现是**成本看板长期少算,而且少算的量随断线频率变化**,没人能对上。

        ## 用户那句为什么也 yield

        `RunningTurn`(见 `start_turn`)只广播这里 yield 出去的块。用户那句如果只落盘
        不 yield,`attach()` 的直播订阅者能不能看到它就要看一次纯属时序的巧合——
        补齐读历史那一刻它落没落盘完全不确定。让"落盘"与"产出"对每一块都成对发生,
        这个不确定性才会彻底消失,不用靠时序侥幸。

        **它必须 yield 在 `try` 块**里,不能在前面。生成器被 `aclose()` 时只有已经进入
        的 `try` 才有对应的 `finally` 会跑——放在 `try` 外面的话,调用方在收到这第一块
        之后就走人,记账会被完整跳过,`test_tokens_are_charged_even_when_the_caller_
        walks_away` 就是照这个坑写的回归用例。
        """
        stamp = now or datetime.now(UTC)
        session = await self.ensure_open(session_id, stamp)
        handle = self._handles[session_id]

        question = log.user_block(session.last_seq + 1, message)
        seq = log.append(self.root, session_id, [question])

        runtime = self._runtime_for(session)
        handle.message_index = self._turn_index(session)
        stream = runtime.send_message(handle, message)
        produced = 0
        # 工具块上那句「用时 4.2s」要真的量出来。用单调时钟:墙钟会被 NTP 调整,而这里
        # 量的是"过了多久",不是"现在几点"。
        began = monotonic()

        try:
            # 用户那一条先落盘再产出:只记不给的话,日志面倒是回答得了"他当时问的是
            # 什么",但正在跟着直播的那一侧永远看不到自己刚发的这句。
            yield question
            async for event in stream:
                for block in blocks_from(
                    [event], start_seq=seq + produced, elapsed_s=monotonic() - began
                ):
                    # **先落盘再 yield。** 反过来的话,推送出去但没落盘的块在断线补齐时
                    # 会凭空消失——而用户明明看见过它。
                    log.append(self.root, session_id, [block])
                    produced += 1
                    yield block

            # 流正常跑完,这时 `turn` 已经是终值。失败要**产出一个错误块**而不只是记日志
            # ——用户看到的是"员工没说话",而它其实是炸了,两者需要完全不同的下一步。
            if not stream.turn.ok:
                failure = Block(
                    seq=seq + produced + 1,
                    kind=BlockKind.ERROR,
                    text=stream.turn.failure_detail or "这一轮没能完成",
                )
                log.append(self.root, session_id, [failure])
                produced += 1
                yield failure
        finally:
            # **账在这里结。** 调用方中途退出时上面走不到,而这一轮 token 已经烧掉了。
            await stream.aclose()
            # 运行时那一侧现在真的有这个会话了,下一轮该走续接。**置位放在 finally 里**,
            # 与记账同一条理由:中途退出时子进程已经把会话建起来了,这时还当作没建的话,
            # 下一条消息会用"新建"参数撞上一个已经存在的标识。
            handle.started = True
            self._settle(
                session_id,
                seq + produced,
                stream.turn.tokens_used,
                stamp,
                native_session_id=stream.turn.native_session_id,
            )

    def _settle(
        self,
        session_id: str,
        seq: int,
        tokens: int,
        stamp: datetime,
        native_session_id: str | None = None,
    ) -> None:
        """结这一轮的账。**从库里重读**,不用生成器开始时那份快照。

        流可能跑了很久,期间会话可能已经被别处改过(挂起、结束)。拿旧快照去存等于把
        那些改动覆盖掉。

        **续接标记与运行时标识也在这里落盘。** 它们只留在内存句柄里的话,重启后即使句柄
        重建成功,也会因为标记丢了而重新走一次"新建会话"参数。
        """
        session = self.store.get(session_id).evolve(
            last_seq=seq,
            tokens_used=self.store.get(session_id).tokens_used + tokens,
            updated_at=stamp,
            started=True,
            # 运行时这一轮报了标识就以它为准;没报就留着原来那份,**不要覆盖成 None**。
            native_session_id=native_session_id or self.store.get(session_id).native_session_id,
        )
        # 预算耗尽自动挂起并写事件——**不静默截断**。
        if session.over_budget and session.state is SessionState.ACTIVE:
            session = session.suspend(SuspendReason.BUDGET, now=stamp)
            self._record(session, "suspended", reason=SuspendReason.BUDGET.value)
        self.store.save(session)

    async def ask(
        self, session_id: str, message: str, now: datetime | None = None
    ) -> list[Block]:
        """把一轮问答收成一个列表。CLI 与测试用——它们不需要边看边等。"""
        return [block async for block in self.send(session_id, message, now=now)]

    def history(self, session_id: str, after: int = 0) -> list[dict[str, object]]:
        """历史块。断线补齐与回放共用它。"""
        self.store.get(session_id)  # 不存在时报错,而不是返回空列表冒充"还没说话"
        return log.read(self.root, session_id, after)

    # --- 后台轮次:发一条消息不再等于占住一次连接 -----------------------------

    def is_generating(self, session_id: str) -> bool:
        """这个会话现在有没有一轮在后台跑。**前端据此决定重新打开页面时要不要接直播。**"""
        return session_id in self._turns

    def start_turn(self, session_id: str, message: str, now: datetime | None = None) -> int:
        """把一轮问答起成后台任务,返回起跑前的 `last_seq`(补齐/直播都从这里起)。

        **同一 session 同一时刻只能有一轮。** 不挡住的话,两轮并发地对着同一个
        `--resume` 句柄发消息,会轮流覆盖对方的 `handle.started` 与运行时会话标识,
        产出交叉错位,而且没有一条错误信息能解释发生了什么——挡在这里,调用方(API 层)
        能把"上一轮还没完"变成一个明确的 409,而不是一堆诡异的后续症状。

        `send()` 本身不动:它仍然是"逐块产出 + `finally` 结账"的那个生成器,这里只是
        换了个跑法——由后台任务把它驱动到底,不再由某一次 HTTP 连接驱动。跑到底这件事
        因此不再依赖任何调用方还在不在场。
        """
        if session_id in self._turns:
            raise SessionRefused(f"会话 {session_id} 上一轮还没说完,等它结束再发下一条。")
        start_seq = self.store.get(session_id).last_seq
        turn = RunningTurn(session_id)
        self._turns[session_id] = turn

        async def runner() -> None:
            try:
                async for block in self.send(session_id, message, now=now):
                    turn.publish(block)
            finally:
                # **先摘注册表,再放行订阅者。** 顺序反过来的话,订阅者收到"结束了"之后
                # 立刻查 `is_generating` 还会看到 True——短暂但真实存在的窗口,足够让一次
                # 页面刷新拿到自相矛盾的状态。
                del self._turns[session_id]
                turn.finish()

        turn.task = asyncio.create_task(runner())
        return start_seq

    async def attach(self, session_id: str, after: int = 0) -> AsyncIterator[Block]:
        """接上一个 session 的块流:先补齐历史,再跟直播(如果这会儿有一轮在跑)。

        补齐和直播不是拼出来的两套逻辑:**先订阅再读历史**,历史读到的块和订阅期间到达
        的块按序号去重——这样"读历史"与"订阅上"之间那段时间差既不会漏一块,也不会让
        同一块被推两遍。这与 `bus.py` 的 `since()` + 订阅是同一个手法。

        没有一轮在跑时(消息已经问完,或者压根没问过),补完历史就结束——调用方看到
        的是一条读完即止的 SSE,不是永远挂着的连接。
        """
        self.store.get(session_id)  # 不存在时报错,理由同 `history`
        turn = self._turns.get(session_id)
        queue = turn.subscribe() if turn is not None else None
        try:
            last = after
            for item in log.read(self.root, session_id, after):
                block = Block.from_dict(item)
                last = block.seq
                yield block
            if queue is None:
                return
            while True:
                # 与历史循环的 `block` 分开命名:队列会给出 `None` 哨兵,
                # 复用同一个名字会把 `Block | None` 塞进已收窄为 `Block` 的变量。
                live = await queue.get()
                if live is None:  # 哨兵:这一轮结束了
                    break
                if live.seq > last:
                    last = live.seq
                    yield live
        finally:
            if turn is not None and queue is not None:
                turn.unsubscribe(queue)

    def stop_turn(self, session_id: str) -> bool:
        """打断正在跑的这一轮。返回值只回答"这会儿是不是真的有一轮在跑"。

        **只取消任务,不在这里结账。** 账仍然在 `send()` 自己的 `finally` 里结——取消
        会让 `send()` 内部的 `async for` 收到 `CancelledError`,同样落到那个 `finally`,
        记账只有一条路径,不会因为"是不是被用户打断的"分叉出第二份逻辑。

        没有一轮在跑时返回 `False` 而不是报错:用户点两下"停止"很正常,第二下不该
        报 404/409——那时候它已经如愿了。
        """
        turn = self._turns.get(session_id)
        if turn is None or turn.task is None:
            return False
        turn.task.cancel()
        return True

    # --- 治理 ----------------------------------------------------------------

    def suspend(self, session_id: str, reason: SuspendReason) -> Session:
        session = self.store.save(self.store.get(session_id).suspend(reason))
        self._record(session, "suspended", reason=reason.value)
        return session

    def resume(
        self, session_id: str, max_tokens: int | None = None, now: datetime | None = None
    ) -> Session:
        """恢复一个已挂起的会话。

        实测确认这不需要任何额外机制:`--resume` 没有短期 TTL,恢复就是再跑一次。

        ## 上限按当前配置重取

        `max_tokens` 给了就用它盖掉会话创建时那份快照。**这不是多此一举**:被预算挂起的
        会话,它的 `tokens_used` 已经越过旧上限了,不重取的话"恢复"会在下一轮结账时原地
        再挂一次——用户看到的是"点了恢复,它只能再说一句就又停了",而他刚刚做的事正是
        去把上限调高。改了配置却救不回那个正卡着的会话,等于配置面板对着唯一的使用场景失效。
        """
        session = self.store.get(session_id)
        if max_tokens is not None and max_tokens != session.max_tokens:
            session = session.evolve(max_tokens=max_tokens)
        session = self.store.save(session.resume(now=now))
        self._record(session, "resumed", max_tokens=session.max_tokens)
        return session

    def end(self, session_id: str) -> Session:
        session = self.store.save(self.store.get(session_id).end())
        self._handles.pop(session_id, None)
        self._record(session, "ended")
        return session

    # --- 内部 ----------------------------------------------------------------

    def _enforce_idle(self, session: Session, now: datetime) -> Session:
        if session.state is SessionState.ACTIVE and session.idle_expired(now):
            suspended = self.store.save(session.suspend(SuspendReason.IDLE, now=now))
            self._record(suspended, "suspended", reason=SuspendReason.IDLE.value)
            return suspended
        return session

    # --- 上下文条 -------------------------------------------------------------

    def pin(self, session_id: str, item: str, pinned: bool = True) -> Session:
        """钉住或取消钉住一条上下文。

        **钉住的不参与截断**:预算紧张时先砍没钉的。用户钉一条的意思就是"这条别丢",
        而系统按命中数排序时并不知道人的判断。
        """
        session = self.store.get(session_id)
        current = set(session.pinned)
        current.add(item) if pinned else current.discard(item)
        return self.store.save(session.evolve(pinned=tuple(sorted(current))))

    def drop_context(self, session_id: str, item: str) -> Session:
        """把一条上下文从这次会话里移掉。

        员工带偏了的时候,用户要能把噪声拿掉。移掉的同时也解钉——留着一条钉在不存在
        条目上的记录只会让下次截断算错。
        """
        session = self.store.get(session_id)
        return self.store.save(
            session.evolve(
                context_items=tuple(x for x in session.context_items if x != item),
                pinned=tuple(x for x in session.pinned if x != item),
            )
        )

    # --- 反馈 -----------------------------------------------------------------

    def feedback(self, session_id: str, useful: bool) -> tuple[str, ...]:
        """对一条回答表态。有用则给这次装载的知识卡片记一次命中。

        **这是「对话本身成为知识自然选择的输入源」唯一的落地点**(§10.2)。没有它,
        那句话只是一句主张。

        只记 `card:` 那些——文件与任务产物不参与自然选择,它们没有"该不该淘汰"这个问题。
        没用则什么都不记:**不倒扣**。一次没帮上忙不等于这张卡片是错的,而倒扣会让
        少数几次不满意把一张长期有用的卡片打下去。
        """
        from agentgenome.genome.hits import CardRef, record_credits

        session = self.store.get(session_id)
        cards = tuple(
            CardRef(*item.removeprefix("card:").split("/", 1))
            for item in session.context_items
            if item.startswith("card:") and "/" in item
        )
        # 返回的是**记了账的**那些,不是"这次装载了哪些卡片"——两者在 useful=False 时
        # 完全不同,而调用方(前端)拿它去显示"已记账 hits +1"。名不副实的返回值会让
        # 界面在没记账的时候也说记了。
        credited = cards if useful else ()
        if credited:
            record_credits(self.root, credited)
        self._record(session, "feedback", useful=useful, cards=[c.feature_id for c in credited])
        return tuple(f"{c.module_id}/{c.feature_id}" for c in credited)

    def _record(self, session: Session, action: str, **extra: object) -> None:
        """把会话生命周期记进事件面。

        **只记动作与标识,不记对话内容**——内容在日志面(`messages.jsonl`)。同一件事
        只由一个平面记录内容,两个平面各存一份必然发散,而发散之后没法判断哪一份是对的。

        关联到 `task_id`(关联了任务的)或会话自己的 id:事件流按 id 检索,没关任务的会话
        挂自己的 id 才查得到。
        """
        self.log.append(
            session.task_id or session.id,
            actor=ORCHESTRATOR,
            kind=LogKind.SESSION,
            payload={
                "session_id": session.id,
                "action": action,
                # **记 `writable` 而不是旧的模式名。** 事件面是审计证据,它要回答的是
                # "这个会话当时能不能改代码"——派生的模式名回答不了。
                "writable": session.writable,
                "employee_id": session.employee_id,
                "state": session.state.value,
                **extra,
            },
        )

    @staticmethod
    def _why_closed(session: Session) -> str:
        """说清楚为什么发不出去。

        只说"会话不可用"的话,用户的第一反应是"它坏了"——而它其实是按规则停的。
        """
        if session.state is SessionState.ENDED:
            return f"会话 {session.id} 已结束,不能再发消息。要接着聊请新建一个会话。"
        reason = {
            SuspendReason.IDLE: "空闲超时",
            SuspendReason.BUDGET: "token 预算耗尽",
        }.get(session.suspend_reason or SuspendReason.IDLE, "未知原因")
        return f"会话 {session.id} 已挂起({reason})。恢复它就能接着聊。"

    def _turn_index(self, session: Session) -> int:
        """这是第几轮。回放靠它定位录制。

        **数的是提问数,不是文本块数。** 后者把用户那条和员工那条混在一起,一轮就长两个,
        第二轮于是去找 `m3` 那份录制——而它不存在。轮次的定义只有一个:这是第几个问题。
        """
        asked = [
            item
            for item in log.read(self.root, session.id)
            if (item.get("detail") or {}).get("role") == "user"
        ]
        return len(asked) or 1

    def _runtime(self, name: str) -> SessionCapableRuntime:
        runtime = self.runtimes.get(name)
        if runtime is None:
            raise SessionsUnsupported(f"运行时 {name} 没有注册")
        if not isinstance(runtime, SessionCapableRuntime):
            raise SessionsUnsupported(f"运行时 {name} 不支持会话")
        return runtime

    def _runtime_for(self, session: Session) -> SessionCapableRuntime:
        """这个会话跑在哪个运行时上。

        **认会话自己记下的那个,不是"随便挑一个支持会话的"。** 早先的实现遍历注册表返回
        第一个有 `send_message` 的,于是混配部署(一部分员工 claude-code、一部分 qwen)里
        第二条消息可能落到另一个运行时上——而它的 `--resume` 指向一个不存在的会话。
        症状是"聊了两句它就不记得了",极难归因。
        """
        return self._runtime(session.runtime)

    def _assemble(
        self,
        employee: EmployeeConfig,
        session: Session,
        question: str,
        crafts: tuple[tuple[str, str], ...] = (),
    ) -> tuple[str, tuple[str, ...]]:
        """给这个会话组一份上下文包。

        **走与自主 Job 同一个 `ContextAssembler`**,不另写一套:两套组装逻辑必然分叉,
        而分叉的表现是"某条路径下员工少看了点东西",没有任何外部症状。

        关联了任务的预载它的产物清单;没关任务的按标题走关键词路由。切不出基因组
        不算失败——一个还没有项目地图的 Workspace 照样该能开会话。
        """
        from agentgenome.context import ContextInputs, assemble, card_refs, load_genome_slice
        from agentgenome.genome.errors import GenomeValidationError

        try:
            genome = load_genome_slice(self.root, ())
        except (GenomeValidationError, OSError):
            genome = ()

        bundle = assemble(
            ContextInputs(
                employee=employee,
                procedure_prompt=session_prompt(session.writable, session.task_id),
                requirement=question,
                plan=self._task_artifacts(session),
                genome=genome,
                crafts=crafts,
            )
        )
        # 装载清单要减掉**被预算砍掉的**那些:把没真的进上下文的卡片列进"已装载"是在骗人,
        # 而用户正是看着这个控件判断该不该采信这次回答。`bundle.dropped` 给的是被砍片段的标题。
        kept = tuple(item for item in genome if item.title not in set(bundle.dropped))
        items = [f"card:{ref.module_id}/{ref.feature_id}" for ref in card_refs(kept)]
        if session.task_id:
            items.insert(0, f"task:{session.task_id}")
        return bundle.text, tuple(items)

    def _task_artifacts(self, session: Session) -> str:
        """关联任务的产物清单。**关了任务就给,与读写权限无关。**

        **列清单而不是把内容全灌进去**:上下文包的本质是"目录 + 已命中内容",员工可以自己
        Read 展开。全量灌入会把一次提问的成本抬到和跑一遍任务差不多。

        任务目录可能已经被清理(复盘已完成的任务是最典型的场景)——那时给审计包的位置。

        ## 路径给绝对的

        清单要能被 `Read` 打开,而**会话的 cwd 不一定是项目根**:可写会话跑在 worktree 里,
        而任务的运行态目录不在任何 worktree 中。早先这里给的是 `relative_to(self.root)`,
        配上当时"会话跑在一个空目录里"的工作区,清单上**每一行都打不开**——员工只能说
        "我看不到这些文件",而清单看起来完全正常。
        """
        if not session.task_id:
            return ""
        directory = self.root / paths.TASKS / session.task_id
        if not directory.is_dir():
            return f"任务 {session.task_id} 的运行态目录已清理,证据见审计包 {paths.ARCHIVE}/。"
        found = sorted(str(p) for p in directory.rglob("*") if p.is_file())
        listing = "\n".join(f"- {item}" for item in found[:200])
        return f"关联任务 {session.task_id} 的产物与日志:\n{listing}"




__all__ = [
    "SessionRefused",
    "SessionService",
    "SessionsUnsupported",
    "session_prompt",
    "workdir_for",
]
