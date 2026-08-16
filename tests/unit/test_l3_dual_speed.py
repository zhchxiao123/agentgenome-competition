"""L3a/L3b 双速率:接口改动与内容改动不再共用一道闸。"""

from __future__ import annotations

import pytest

from agentgenome.genome.evolution.cards import Level
from agentgenome.genome.evolution.proposals import (
    ProcedureProposal,
    RegressionMissing,
    RegressionResult,
    classify_l3,
    needs_human_approval,
)


class TestClassify:
    @pytest.mark.parametrize(
        "path",
        [
            "genome/procedures/unit-gate/craft/log-forensics/SKILL.md",
            "genome/procedures/_common/craft/genome-navigation/SKILL.md",
            "craft/a/SKILL.md",
        ],
    )
    def test_changes_inside_craft_are_content_level(self, path: str) -> None:
        assert classify_l3([path]) is Level.L3B

    @pytest.mark.parametrize(
        "path",
        [
            "genome/procedures/unit-gate/procedure.yaml",
            "genome/procedures/unit-gate/prompt.md",
            "genome/procedures/unit-gate/scripts/run.py",
            "genome/procedures/unit-gate/schemas/out.json",
        ],
    )
    def test_everything_outside_craft_is_interface_level(self, path: str) -> None:
        """scripts/ 与 schemas/ 同样算接口级——它们改变编排器看到的契约或确定性行为。"""
        assert classify_l3([path]) is Level.L3A

    def test_a_mixed_proposal_takes_the_stricter_lane(self) -> None:
        """只要在一堆手艺改动里夹一行 procedure.yaml,整个提案就能走快通道溜过去
        ——而那正是这道闸要防的事。
        """
        assert (
            classify_l3(
                [
                    "genome/procedures/x/craft/a/SKILL.md",
                    "genome/procedures/x/craft/b/SKILL.md",
                    "genome/procedures/x/procedure.yaml",
                ]
            )
            is Level.L3A
        )

    def test_an_empty_change_set_is_not_waved_through(self) -> None:
        """没有路径就没有依据。默认走严的那档,不默认放行。"""
        assert classify_l3([]) is Level.L3A

    def test_windows_style_separators_classify_the_same(self) -> None:
        assert classify_l3([r"genome\procedures\x\craft\a\SKILL.md"]) is Level.L3B


class TestApprovalLane:
    def test_interface_changes_need_a_human(self) -> None:
        assert needs_human_approval(Level.L3A) is True

    def test_content_changes_do_not(self) -> None:
        assert needs_human_approval(Level.L3B) is False

    def test_rules_still_need_a_human(self) -> None:
        """L2 不受这次拆分影响——规则层永远握在人手里。"""
        assert needs_human_approval(Level.L2) is True


class TestProposal:
    def _proposal(self, paths: tuple[str, ...], regressed: bool = False) -> ProcedureProposal:
        return ProcedureProposal(
            procedure_id="unit-gate",
            reason="反复以同一种方式失败",
            diff="+ 一行",
            regression=RegressionResult(samples=5, before_pass=3, after_pass=1 if regressed else 5),
            changed_paths=paths,
        )

    def test_a_craft_only_proposal_skips_the_approval_queue(self) -> None:
        proposal = self._proposal(("genome/procedures/x/craft/a/SKILL.md",))

        assert proposal.level is Level.L3B
        assert proposal.needs_human is False

    def test_a_procedure_proposal_goes_to_the_queue(self) -> None:
        proposal = self._proposal(("genome/procedures/x/prompt.md",))

        assert proposal.level is Level.L3A
        assert proposal.needs_human is True

    def test_the_fast_lane_still_requires_regression_evidence(self) -> None:
        """快通道换掉的是"要不要人看",不是"要不要验证"。"""
        proposal = ProcedureProposal(
            procedure_id="unit-gate",
            reason="",
            diff="+ 一行",
            regression=None,
            changed_paths=("genome/procedures/x/craft/a/SKILL.md",),
        )

        assert proposal.needs_human is False
        with pytest.raises(RegressionMissing):
            proposal.ensure_mergeable()

    def test_a_craft_change_that_made_things_worse_cannot_merge(self) -> None:
        proposal = self._proposal(("genome/procedures/x/craft/a/SKILL.md",), regressed=True)

        with pytest.raises(RegressionMissing):
            proposal.ensure_mergeable()

    def test_the_rendered_proposal_names_its_lane(self) -> None:
        """审批人打开 PR 要一眼看出这条走哪条通道,不必自己推。"""
        fast = self._proposal(("genome/procedures/x/craft/a/SKILL.md",)).render()
        slow = self._proposal(("genome/procedures/x/procedure.yaml",)).render()

        assert "快通道" in fast and "L3b" in fast
        assert "慢通道" in slow and "L3a" in slow
