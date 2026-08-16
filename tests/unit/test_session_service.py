"""会话服务:穿过回放缝跑真实的落盘、预算、挂起与恢复。

**不为会话另造一个 mock 运行时**——那会让会话测试与 Job 测试用两套假实现,而两套假实现
迟早行为分叉。这里用的是 `ReplayRuntime`,与全仓其余测试同一条缝。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentgenome.agents.recording import RecordingLibrary, session_key
from agentgenome.agents.replay import ReplayRuntime
from agentgenome.employees import EmployeeConfig
from agentgenome.sessions import log
from agentgenome.sessions.model import (
    DEFAULT_MAX_TOKENS,
    READONLY_TOOLS,
    SessionState,
    SuspendReason,
)
from agentgenome.sessions.service import SessionRefused, SessionService, workdir_for
from agentgenome.sessions.store import SessionStore
from agentgenome.space.git_ws import WORKTREES_HOME_ENV, GitWorkspace

NOW = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)


def _failing(detail: str) -> object:
    """一个每轮都失败的运行时。**只替换 `send_message`**——会话已经开好了,要模拟的是
    "开好之后某一轮炸了",不是"开不起来"。
    """

    from collections.abc import AsyncIterator

    from agentgenome.agents.runtime import SessionStream, SessionTurn

    class Failing:
        name = "replay"

        async def start_session(self, spec: object) -> object:  # pragma: no cover
            raise AssertionError("会话已经开好了,不该再调这个")

        def send_message(self, handle: object, message: str) -> SessionStream:
            turn = SessionTurn(ok=False, failure_detail=detail)

            async def nothing() -> AsyncIterator[object]:
                return
                yield  # pragma: no cover - 让它成为生成器

            return SessionStream(nothing(), turn)

    return Failing()


def _pausing(resume: asyncio.Event) -> object:
    """一个卡在第一块之后不动的运行时,直到测试放行(或者被取消)。

    `ReplayRuntime` 没有真实的 I/O,一轮问答一旦起跑就在同一个调度步里跑到底——用它
    没法确定性地抓到"这一轮正卡在半路"这个时刻,只能赌真实时序,而这类测试一赌就会
    偶发失败。这里用一个 `asyncio.Event` 当遥控器,测试说了算什么时候该继续。
    """

    from collections.abc import AsyncIterator

    from agentgenome.agents.events import EventKind, NormalizedEvent
    from agentgenome.agents.runtime import SessionStream, SessionTurn

    class Pausing:
        name = "replay"

        async def start_session(self, spec: object) -> object:  # pragma: no cover
            raise AssertionError("会话已经开好了,不该再调这个")

        def send_message(self, handle: object, message: str) -> SessionStream:
            turn = SessionTurn()

            async def stream() -> AsyncIterator[NormalizedEvent]:
                try:
                    yield NormalizedEvent(
                        kind=EventKind.TOOL_USE, text="读点什么", detail={"name": "Read"}
                    )
                    await resume.wait()
                    yield NormalizedEvent(kind=EventKind.TEXT, text="答案")
                finally:
                    # 账在 finally 里结,不管是正常走完还是被打断——与 `ReplayRuntime` 同一
                    # 条理由。
                    turn.tokens_used = 77

            return SessionStream(stream(), turn)

    return Pausing()


def _employee(employee_id: str = "architect") -> EmployeeConfig:
    return EmployeeConfig(
        id=employee_id,
        runtime="replay",
        prompt="prompts/x.md",
        procedures=["code-develop"],
        tools={"allow": ["Bash", "Read", "Write", "Edit"], "deny": []},
    )


def _record(library: Path, employee_id: str, session_id: str, index: int, text: str) -> None:
    directory = library / session_key(employee_id, session_id, index)
    directory.mkdir(parents=True, exist_ok=True)
    events = [
        {"kind": "tool_use", "text": "genome/knowledge/x.md", "detail": {"name": "Read"}},
        {"kind": "text", "text": text},
        {"kind": "usage", "usage": {"input_tokens": 100, "output_tokens": 50}},
    ]
    (directory / "stream.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionService:
    library = tmp_path / "lib"
    library.mkdir()
    return SessionService(
        workspace_root=tmp_path / "ws",
        store=SessionStore(tmp_path / "ws"),
        runtimes={"replay": ReplayRuntime(RecordingLibrary(library))},
    )


class TestWorkdir:
    def test_a_read_only_session_runs_in_the_project_root(self, tmp_path: Path) -> None:
        """**它必须看得见代码。**

        早先只读会话跑在一个空目录里(躲开 worktree 的生命周期),而上下文包里不含路径、
        命令行里没有 `--add-dir`、工具集里没有 Bash——员工的 Grep 搜的是一个空文件夹,
        开场白却告诉它"基于项目的代码回答"。项目根同时满足"看得见代码"与"不会被清理"。
        """
        root = tmp_path / "ws"
        worktree = tmp_path / "worktrees" / "ag-1"

        assert workdir_for(root, "sess-1", False, None) == root
        # 关了任务也一样:只读会话不进任何 worktree,免得跟着它一起被清理。
        assert workdir_for(root, "sess-1", False, worktree) == root

    def test_a_writable_session_on_a_task_runs_in_that_task_worktree(
        self, tmp_path: Path
    ) -> None:
        """workdir 就是 worktree,**其会话寿命因此等于任务**——有意为之。"""
        worktree = tmp_path / "worktrees" / "ag-1"

        assert workdir_for(tmp_path / "ws", "sess-1", True, worktree) == worktree

    def test_a_writable_session_without_a_task_gets_its_own_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """改动要有地方落,而那个地方**不能是主线的工作树**。"""
        monkeypatch.setenv(WORKTREES_HOME_ENV, str(tmp_path / "wt"))
        root = tmp_path / "ws"
        root.mkdir()

        workdir = workdir_for(root, "sess-1", True, None)

        assert workdir == GitWorkspace(root).session_worktree_path("sess-1")
        assert workdir != root


class TestCraftsOnTheProjectRoot:
    """**只读会话跑在项目根上,而那里不是一次性的派生视图——它是用户的仓库。**"""

    async def test_the_repository_own_craft_mount_is_not_wiped(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """`craft.materialize` 会先 `rmtree` 掉 `.claude/skills` 再复制。

        那个"先清空"对 worktree 是对的(残留会让"这个员工只看得到自己的手艺"不成立),
        但在项目根上执行同一套动作,删掉的是**仓库自己的**手艺目录——与这次会话毫无关系,
        而且没有任何东西会报错。
        """
        root = tmp_path / "ws"
        mine = root / ".claude" / "skills" / "my-own" / "SKILL.md"
        mine.parent.mkdir(parents=True)
        mine.write_text("# 仓库自己的手艺\n", encoding="utf-8")

        await service.create(_employee(), now=NOW)

        assert mine.read_text(encoding="utf-8") == "# 仓库自己的手艺\n"

    async def test_the_crafts_are_inlined_into_the_bundle_instead(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """不物化不等于不给。**"员工没带手艺"是最难归因的一类现象**——它没有任何报错,
        只表现为"最近它好像变笨了"。降级路径直接借 Job 那一条:手艺全文内联进上下文包。
        """
        root = tmp_path / "ws"
        manifest = root / "genome" / "procedures" / "_common" / "craft" / "写测试" / "SKILL.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("# 怎么写测试\n先写红的。\n", encoding="utf-8")
        employee = EmployeeConfig(
            id="architect",
            runtime="replay",
            prompt="prompts/x.md",
            procedures=["code-develop"],
            tools={"allow": ["Read"], "deny": []},
            crafts=["写测试"],
        )

        session = await service.create(employee, now=NOW)

        bundle = log.context_path(root, session.id, 1).read_text(encoding="utf-8")
        assert "先写红的" in bundle


class TestWritableWorkspaceIsExclusive:
    async def test_a_second_writable_session_on_the_same_task_is_refused(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """两个可写会话开在同一个任务上会拿到**同一个 cwd**,两个进程同时改同一批文件。

        `start_turn` 那把锁只管一个会话内部的轮次,跨会话什么都不挡。这个洞一直都在,只是
        被"结对会话通过界面根本建不出来"掩盖着。
        """
        worktree = tmp_path / "wt" / "ag-1"
        worktree.mkdir(parents=True)
        first = await service.create(
            _employee(), writable=True, task_id="ag-1", task_worktree=worktree, now=NOW
        )

        with pytest.raises(SessionRefused, match=first.id):
            await service.create(
                _employee(), writable=True, task_id="ag-1", task_worktree=worktree, now=NOW
            )

    async def test_ending_the_first_one_frees_the_workspace(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        worktree = tmp_path / "wt" / "ag-1"
        worktree.mkdir(parents=True)
        first = await service.create(
            _employee(), writable=True, task_id="ag-1", task_worktree=worktree, now=NOW
        )
        service.end(first.id)

        second = await service.create(
            _employee(), writable=True, task_id="ag-1", task_worktree=worktree, now=NOW
        )

        assert second.writable is True

    async def test_read_only_sessions_do_not_compete(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """只读会话拿不到写工具,同处一个目录不会互相踩——挡住它们只是白白拦人。"""
        await service.create(_employee(), task_id="ag-1", now=NOW)
        second = await service.create(_employee(), task_id="ag-1", now=NOW)

        assert second.writable is False


class TestCreate:
    async def test_a_read_only_session_starts_active(self, service: SessionService) -> None:
        session = await service.create(_employee(), now=NOW)

        assert session.state is SessionState.ACTIVE
        assert session.writable is False

    async def test_neither_a_task_nor_write_access_is_required(
        self, service: SessionService
    ) -> None:
        """**两个都不给是完全正常的一种**:在项目根上只读地聊。早先没有任务的质询会被拒,
        而那条校验的前提是"质询"作为一种模式存在。
        """
        session = await service.create(_employee(), now=NOW)

        assert session.task_id is None

    async def test_read_only_tools_are_pinned_not_inherited(self, service: SessionService) -> None:
        """员工定义里的 allow 是为写代码准备的,继承过来就等于只读模式名不副实。"""
        session = await service.create(_employee(), now=NOW)

        assert session.tools == READONLY_TOOLS
        assert "Write" not in session.tools

    async def test_a_session_is_unlimited_unless_the_project_says_otherwise(
        self, service: SessionService
    ) -> None:
        """**缺省不拦。**

        内置常数猜过两次都猜错了(20k、50k),而只读会话搬到项目根之后实测一轮就能烧掉
        370k——任何一个猜出来的数都会让用户在发出第一句之后就被挂起,挂起理由只写
        "token 预算耗尽",既看不出这是拍脑袋定的,也看不出去哪儿改。要设由用它的人设。
        """
        session = await service.create(_employee(), task_id="ag-1", now=NOW)

        assert session.max_tokens == DEFAULT_MAX_TOKENS == 0
        assert session.over_budget is False

    async def test_an_unlimited_session_never_counts_as_over_budget(
        self, service: SessionService
    ) -> None:
        """0 是"没有上限",不是"上限是 0"——判反了的话第一轮就判超预算。"""
        session = await service.create(_employee(), now=NOW)

        assert session.evolve(tokens_used=10_000_000).over_budget is False

    async def test_the_caller_decides_the_budget(self, service: SessionService) -> None:
        """上限从项目配置来,由 REST 面取好传进来——这一层不读配置。"""
        session = await service.create(_employee(), max_tokens=1234, now=NOW)

        assert session.max_tokens == 1234

    async def test_the_context_bundle_lands_on_disk(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """上下文包落盘,保证任意一轮可离线复现。"""
        session = await service.create(
            _employee(), context="# 上下文\n", now=NOW
        )

        assert log.context_path(tmp_path / "ws", session.id, 1).read_text("utf-8") == "# 上下文\n"


class TestSend:
    async def _session(self, service: SessionService, tmp_path: Path):
        session = await service.create(_employee(), now=NOW)
        _record(tmp_path / "lib", "architect", session.id, 1, "补偿逻辑是这样的")
        return session

    async def test_a_turn_produces_blocks(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        session = await self._session(service, tmp_path)

        blocks = await service.ask(session.id, "补偿逻辑是什么?", now=NOW)

        assert [block.kind.value for block in blocks] == ["text", "tool-step", "text"]

    async def test_everything_lands_in_the_log_including_the_question(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """只记员工的回答的话,日志面回答不了「他当时问的是什么」。"""
        session = await self._session(service, tmp_path)

        await service.ask(session.id, "补偿逻辑是什么?", now=NOW)

        texts = [item.get("text", "") for item in service.history(session.id)]
        assert "补偿逻辑是什么?" in texts
        assert "补偿逻辑是这样的" in texts

    async def test_block_sequence_numbers_are_monotonic(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """断线补齐靠它。"""
        session = await self._session(service, tmp_path)

        await service.ask(session.id, "问题", now=NOW)

        seqs = [item["seq"] for item in service.history(session.id)]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    async def test_history_after_a_sequence_matches_the_tail(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """补齐结果必须与完整流的尾巴逐块相等,否则重连之后内容会错位。"""
        session = await self._session(service, tmp_path)
        await service.ask(session.id, "问题", now=NOW)

        whole = service.history(session.id)
        tail = service.history(session.id, after=whole[0]["seq"])

        assert tail == whole[1:]

    async def test_tokens_are_accounted_against_the_session(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        session = await self._session(service, tmp_path)

        await service.ask(session.id, "问题", now=NOW)

        assert service.store.get(session.id).tokens_used == 150

    async def test_an_unknown_session_is_reported(self, service: SessionService) -> None:
        with pytest.raises(LookupError):
            await service.ask("nope", "问题", now=NOW)


class TestGovernance:
    async def _session(self, service: SessionService, tmp_path: Path):
        session = await service.create(_employee(), now=NOW)
        for index in (1, 2):
            _record(tmp_path / "lib", "architect", session.id, index, f"第 {index} 轮")
        return session

    async def test_an_idle_session_suspends_itself_with_a_reason(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """只显示「已挂起」不说原因,用户的第一反应是「它坏了」。"""
        session = await self._session(service, tmp_path)
        later = NOW + timedelta(minutes=31)

        with pytest.raises(SessionRefused, match="空闲超时"):
            await service.ask(session.id, "问题", now=later)

        stored = service.store.get(session.id)
        assert stored.state is SessionState.SUSPENDED
        assert stored.suspend_reason is SuspendReason.IDLE

    async def test_a_suspended_session_can_be_resumed_and_keeps_going(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """实测:`--resume` 没有短期 TTL,恢复不需要任何额外机制。"""
        session = await self._session(service, tmp_path)
        service.suspend(session.id, SuspendReason.IDLE)

        service.resume(session.id, now=NOW)
        blocks = await service.ask(session.id, "接着问", now=NOW)

        assert blocks
        assert service.store.get(session.id).state is SessionState.ACTIVE

    async def test_resuming_retakes_the_budget_from_the_project(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """被预算挂起的会话,恢复时要按**当前**配置重取上限。

        不重取的话它的 `tokens_used` 还压在旧上限之上,下一轮结账原地再挂一次——用户看到
        "点了恢复,它只能再说一句就又停了",而他刚做的事正是去把上限调高。改了配置却救不回
        那个正卡着的会话,等于配置面板对着唯一的使用场景失效。
        """
        session = await service.create(_employee(), max_tokens=50_000, now=NOW)
        service.store.save(service.store.get(session.id).evolve(tokens_used=376_000))
        service.suspend(session.id, SuspendReason.BUDGET)

        resumed = service.resume(session.id, max_tokens=0, now=NOW)

        assert resumed.max_tokens == 0
        assert resumed.over_budget is False

    async def test_resuming_without_a_new_budget_keeps_the_old_one(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """不给就不动。**恢复不是偷偷重设的时机**——CLI 与测试直接调它时不该有意外副作用。"""
        session = await service.create(_employee(), max_tokens=50_000, now=NOW)
        service.suspend(session.id, SuspendReason.IDLE)

        assert service.resume(session.id, now=NOW).max_tokens == 50_000

    async def test_running_out_of_budget_suspends_rather_than_truncating(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """超限自动挂起并写事件——**不静默截断**。"""
        session = await self._session(service, tmp_path)
        service.store.save(service.store.get(session.id).evolve(max_tokens=100))

        await service.ask(session.id, "问题", now=NOW)

        stored = service.store.get(session.id)
        assert stored.state is SessionState.SUSPENDED
        assert stored.suspend_reason is SuspendReason.BUDGET

    async def test_an_ended_session_says_so_rather_than_failing_vaguely(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        session = await self._session(service, tmp_path)
        service.end(session.id)

        with pytest.raises(SessionRefused, match="已结束"):
            await service.ask(session.id, "问题", now=NOW)

    async def test_the_permission_cannot_be_changed_after_creation(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """只读与可写是两套工具集,中途切换等于一次没有闸门的提权。"""
        session = await self._session(service, tmp_path)

        with pytest.raises(ValueError, match="读写权限"):
            service.store.get(session.id).evolve(writable=True)


class TestFailedTurns:
    """一轮失败时用户必须看得出来。

    只把 `turn.ok` 丢掉的话,超时会表现为"员工没说话"——而"没说话"与"炸了"需要完全不同的
    下一步。
    """

    async def test_a_failed_turn_surfaces_as_an_error_block(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        session = await service.create(_employee(), now=NOW)

        service.runtimes["replay"] = _failing("超过 600 秒未结束")

        blocks = await service.ask(session.id, "问题", now=NOW)

        assert [block.kind.value for block in blocks] == ["text", "error"]
        assert "600 秒" in blocks[1].text

    async def test_the_error_lands_in_the_log_too(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """失败也要留痕——复盘时"这一轮为什么没有回答"要能查到。"""
        session = await service.create(_employee(), now=NOW)

        service.runtimes["replay"] = _failing("进程起不来")
        await service.ask(session.id, "问题", now=NOW)

        kinds = [item.get("kind") for item in service.history(session.id)]
        assert "error" in kinds


class TestEventPlane:
    """三平面对会话没有例外。"""

    async def test_creating_a_session_lands_on_the_event_plane(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        session = await service.create(_employee(), now=NOW)

        events = list(service.log.events(session.id))

        assert [e.payload.get("action") for e in events] == ["created"]

    async def test_suspending_records_the_reason_on_the_event_plane(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        session = await service.create(_employee(), now=NOW)
        service.suspend(session.id, SuspendReason.IDLE)

        payloads = [e.payload for e in service.log.events(session.id)]

        assert payloads[-1]["action"] == "suspended"
        assert payloads[-1]["reason"] == "idle"

    async def test_the_event_carries_no_conversation_content(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """同一件事只由一个平面记录内容。内容在日志面,事件面只记动作与标识。"""
        session = await service.create(_employee(), now=NOW)

        [event] = list(service.log.events(session.id))

        assert set(event.payload) == {"session_id", "action", "writable", "employee_id", "state"}


class TestStreaming:
    """真流式:块边产边给,而账无论怎么退出都结得上。"""

    async def _session(self, service: SessionService, tmp_path: Path):
        session = await service.create(_employee(), now=NOW)
        _record(tmp_path / "lib", "architect", session.id, 1, "补偿逻辑是这样的")
        return session

    async def test_blocks_arrive_one_at_a_time(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """攒完再一次性回放的话,用户看到的是"卡住很久然后突然全出来"。"""
        session = await self._session(service, tmp_path)

        seen: list[str] = []
        async for block in service.send(session.id, "问题", now=NOW):
            seen.append(block.kind.value)

        assert seen == ["text", "tool-step", "text"]

    async def test_each_block_is_on_disk_before_it_is_handed_out(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """先落盘再 yield:推送出去但没落盘的块在断线补齐时会凭空消失,而用户明明看见过它。"""
        session = await self._session(service, tmp_path)

        async for block in service.send(session.id, "问题", now=NOW):
            landed = [item["seq"] for item in service.history(session.id)]
            assert block.seq in landed, "这一块还没落盘就被推出去了"

    async def test_tokens_are_charged_even_when_the_caller_walks_away(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """**这条是这次改动最要紧的回归防线。**

        客户端断线、用户按停止——那一刻 token 已经烧掉了。不结账的表现是成本看板长期
        少算,而且少算的量随断线频率变化,没人能对上。
        """
        session = await self._session(service, tmp_path)

        stream = service.send(session.id, "问题", now=NOW)
        await anext(stream)  # 只收第一块就走人
        await stream.aclose()

        assert service.store.get(session.id).tokens_used == 150

    async def test_walking_away_still_advances_the_sequence(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """断在半路时 `last_seq` 也要跟上,否则下一轮的序号会和已落盘的块撞。"""
        session = await self._session(service, tmp_path)

        stream = service.send(session.id, "问题", now=NOW)
        await anext(stream)
        await stream.aclose()

        stored = service.store.get(session.id)
        landed = [item["seq"] for item in service.history(session.id)]
        assert stored.last_seq >= max(landed)

    async def test_a_second_round_does_not_reuse_sequence_numbers(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        session = await self._session(service, tmp_path)
        _record(tmp_path / "lib", "architect", session.id, 2, "第二轮")
        await service.ask(session.id, "第一问", now=NOW)

        await service.ask(session.id, "第二问", now=NOW)

        seqs = [item["seq"] for item in service.history(session.id)]
        assert len(set(seqs)) == len(seqs), f"序号重复了: {seqs}"


class TestBackgroundTurns:
    """一轮问答跑在后台任务里,不再绑定任何一次 `attach()` 连接。

    这是这次改动的核心行为:关掉页面不再打断生成,重新打开能接上——这些用户可见的
    效果,底层就是这几条不变式。
    """

    async def _session(self, service: SessionService, tmp_path: Path):
        session = await service.create(_employee(), now=NOW)
        _record(tmp_path / "lib", "architect", session.id, 1, "补偿逻辑是这样的")
        return session

    async def test_two_readers_attached_to_the_same_turn_see_the_same_blocks(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """两个标签页(或者一个刷新前、一个刷新后)同时接同一轮,不该各看到一半。"""
        session = await self._session(service, tmp_path)
        service.start_turn(session.id, "问题", now=NOW)

        async def collect() -> list[str]:
            return [block.kind.value async for block in service.attach(session.id, after=0)]

        first, second = await asyncio.gather(collect(), collect())
        assert first == second == ["text", "tool-step", "text"]

    async def test_a_second_turn_is_refused_while_one_is_running(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """不挡住的话,两轮并发地对着同一个 `--resume` 句柄发消息,产出会交叉错位。"""
        session = await self._session(service, tmp_path)
        service.start_turn(session.id, "第一问", now=NOW)

        with pytest.raises(SessionRefused):
            service.start_turn(session.id, "第二问", now=NOW)

    async def test_generating_flips_back_to_false_once_the_turn_ends(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        session = await self._session(service, tmp_path)

        assert not service.is_generating(session.id)
        service.start_turn(session.id, "问题", now=NOW)
        assert service.is_generating(session.id)

        async for _ in service.attach(session.id, after=0):
            pass

        assert not service.is_generating(session.id)

    async def test_attaching_after_the_turn_already_ended_just_replays_history(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """没有一轮在跑时,`attach` 补完历史就该结束,不是永远挂着的连接。"""
        session = await self._session(service, tmp_path)
        await service.ask(session.id, "问题", now=NOW)

        replayed = [block async for block in service.attach(session.id, after=0)]

        assert [b.kind.value for b in replayed] == ["text", "tool-step", "text"]

    async def test_reattaching_midway_does_not_miss_or_duplicate_blocks(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """重新打开页面这个场景的核心不变式:先拿到的几块 + 后接上的直播,首尾正好接上。"""
        session = await self._session(service, tmp_path)
        service.start_turn(session.id, "问题", now=NOW)

        first_reader = service.attach(session.id, after=0)
        first_block = await anext(first_reader)
        await first_reader.aclose()

        rest = [block async for block in service.attach(session.id, after=first_block.seq)]

        seqs = [first_block.seq, *[b.seq for b in rest]]
        assert seqs == sorted(set(seqs)), f"补齐/直播接口漏了或者重复了: {seqs}"

    async def test_stopping_a_turn_cancels_it_and_still_settles_the_tokens(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """打断不是把账抹掉——那一刻 token 已经烧掉了,道理与「调用方走人」完全一样。"""
        session = await service.create(_employee(), now=NOW)
        resume = asyncio.Event()
        service.runtimes["replay"] = _pausing(resume)
        service.start_turn(session.id, "问题", now=NOW)

        # 收到工具块就说明这一轮真的卡在半路(`_pausing` 在这之后才等遥控器),这时候
        # 打断它才是在测"正在跑的时候打断",而不是赌一个真实时序。
        reader = service.attach(session.id, after=0)
        async for block in reader:
            if block.kind.value == "tool-step":
                break
        await reader.aclose()

        assert service.is_generating(session.id)
        assert service.stop_turn(session.id) is True
        for _ in range(1000):
            if not service.is_generating(session.id):
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("stop_turn 没能让这一轮真的结束")

        assert service.store.get(session.id).tokens_used == 77

    async def test_stopping_when_nothing_is_running_is_not_an_error(
        self, service: SessionService, tmp_path: Path
    ) -> None:
        """用户点两下"停止"很正常——第二下它已经如愿了,不该报错。"""
        session = await self._session(service, tmp_path)

        assert service.stop_turn(session.id) is False
