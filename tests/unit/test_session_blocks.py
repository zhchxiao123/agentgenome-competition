"""归一化事件 → 前端块的映射。

**后端不为前端造格式。** 块协议来自运行时 stream-json 事件的映射,映射表住在服务端,
前端只认块。这一层的测试盯的是映射本身,不是渲染。
"""

from __future__ import annotations

from agentgenome.agents.events import EventKind, NormalizedEvent
from agentgenome.sessions.blocks import BlockKind, blocks_from


def _text(text: str) -> NormalizedEvent:
    return NormalizedEvent(kind=EventKind.TEXT, text=text)


def _tool(name: str, text: str = "") -> NormalizedEvent:
    return NormalizedEvent(kind=EventKind.TOOL_USE, text=text, detail={"name": name})


class TestFourKinds:
    """**思考 / 正文 / 工具调用 / 子员工** 是四件性质不同的东西,归一化这一层就要分开。

    早先 `thinking` 被映射成 `TEXT`,于是"它在盘算什么"与"它的答复是什么"在这一层就已经
    合并了——下游想区分也无从下手,只能把一整段内心独白当正文渲染给用户看。
    """

    def test_thinking_is_not_prose(self) -> None:
        [block] = blocks_from(
            [NormalizedEvent(kind=EventKind.THINKING, text="先看看有没有现成的手艺")],
            start_seq=0,
        )

        assert block.kind is BlockKind.THINKING
        assert block.kind is not BlockKind.TEXT

    def test_a_sub_agent_block_says_who_produced_it(self) -> None:
        """**"谁产出的"与"这是哪一类"正交。** 子员工自己也会思考、也会调工具、也会说话,
        所以它只能是块上的一条附加信息,不能做成第四种块类型——做成类型的话,"子员工的
        思考"与"子员工的正文"就没有位置了。
        """
        events = [
            NormalizedEvent(
                kind=EventKind.THINKING,
                text="我来查",
                detail={"parent_tool_use_id": "toolu_1"},
            ),
            NormalizedEvent(
                kind=EventKind.TOOL_USE,
                text="src/",
                detail={"name": "Grep", "parent_tool_use_id": "toolu_1"},
            ),
        ]

        thinking, tool = blocks_from(events, start_seq=0)

        assert thinking.kind is BlockKind.THINKING
        assert tool.kind is BlockKind.TOOL_STEP
        assert thinking.detail["parent_tool_use_id"] == "toolu_1"
        assert tool.detail["parent_tool_use_id"] == "toolu_1"

    def test_a_top_level_block_claims_no_parent(self) -> None:
        """没有这个键 = 主线自己干的。给一个空串的话,前端要多判一次"空算不算有"。"""
        [block] = blocks_from([_text("答复")], start_seq=0)

        assert "parent_tool_use_id" not in block.detail

    def test_a_tool_call_carries_its_identity_so_results_can_be_matched_back(self) -> None:
        """子员工的 `parent_tool_use_id` 指向的正是这个值——这条链靠它接起来。"""
        [block] = blocks_from(
            [
                NormalizedEvent(
                    kind=EventKind.TOOL_USE,
                    text="子任务",
                    detail={"name": "Task", "tool_use_id": "toolu_1"},
                )
            ],
            start_seq=0,
        )

        assert block.detail["tool_use_id"] == "toolu_1"


class TestMapping:
    def test_text_becomes_a_text_block(self) -> None:
        [block] = blocks_from([_text("补偿逻辑是这样的")], start_seq=0)

        assert block.kind is BlockKind.TEXT
        assert block.text == "补偿逻辑是这样的"

    def test_a_tool_call_becomes_a_tool_step(self) -> None:
        """工具调用过程可视化是信任的来源:用户看见员工真的在查证,而非凭空作答。"""
        [block] = blocks_from([_tool("Read", "genome/knowledge/x.md")], start_seq=0)

        assert block.kind is BlockKind.TOOL_STEP
        assert block.detail["name"] == "Read"

    def test_a_tool_step_names_what_it_touched(self) -> None:
        """实现成笼统的「正在思考…」这个块就白做了——它必须显示具体在读什么。"""
        [block] = blocks_from([_tool("Read", "src/order/timeout.py")], start_seq=0)

        assert "src/order/timeout.py" in block.text

    def test_usage_events_do_not_become_blocks(self) -> None:
        """用量是记账,不是给用户看的内容。"""
        usage = NormalizedEvent(kind=EventKind.USAGE, usage={"input_tokens": 10})

        assert blocks_from([usage], start_seq=0) == []

    def test_an_error_becomes_an_inline_error_block(self) -> None:
        """错误块内联呈现,不弹全局 toast 打断心流。"""
        error = NormalizedEvent(kind=EventKind.ERROR, text="超时了")

        [block] = blocks_from([error], start_seq=0)

        assert block.kind is BlockKind.ERROR

    def test_empty_text_is_dropped_rather_than_rendered_blank(self) -> None:
        assert blocks_from([_text("   ")], start_seq=0) == []


class TestSequence:
    def test_blocks_carry_monotonic_sequence_numbers(self) -> None:
        """断线补齐靠它。没有序号的话前端只能从头拉一遍。"""
        found = blocks_from([_text("a"), _tool("Read"), _text("b")], start_seq=0)

        assert [block.seq for block in found] == [1, 2, 3]

    def test_sequence_continues_from_where_the_session_left_off(self) -> None:
        found = blocks_from([_text("a"), _text("b")], start_seq=7)

        assert [block.seq for block in found] == [8, 9]


class TestSerialization:
    def test_a_block_round_trips_through_json(self) -> None:
        """块要落盘到 messages.jsonl,再从那里读回来补齐。"""
        [block] = blocks_from([_tool("Grep", "compensate")], start_seq=0)

        assert BlockKind(block.as_dict()["kind"]) is BlockKind.TOOL_STEP
        assert block.as_dict()["seq"] == 1

    def test_the_block_vocabulary_matches_the_design(self) -> None:
        """块类型是对外契约,前端按它注册渲染器。少一个前端就得降级成纯文本。"""
        assert {kind.value for kind in BlockKind} == {
            "text",
            "thinking",
            "code",
            "card-ref",
            "file-ref",
            "diff",
            "tool-step",
            "task-card",
            "gate-report",
            "action",
            "error",
        }
