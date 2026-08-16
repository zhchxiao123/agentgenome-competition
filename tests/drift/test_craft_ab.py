"""手艺的 A/B 基准:一份手艺凭什么值得留在库里。

## 为什么这条默认跳过

它衡量的是**内容质量**,不是正确性。要跑真实 Agent,慢、贵、每次结果不完全一样。
进常规 CI 只会因为大模型的随机性不停假失败,然后被人加上 `--ignore` 从此形同虚设。

手工触发:`pytest tests/drift/test_craft_ab.py -m craft_ab --runslow`,与 PRD 16 的
一致性套件、模型漂移检测走同一条路。

## 判定阈值(这是这份文件真正的交付物)

PRD 里只写了"对比修复轮次与门禁一次通过率",没有定线——**没有线的门等于没有门**,
15 份手艺会全部以"看起来更好"入库。这里定死:

| 指标 | 阈值 | 为什么是这个数 |
|---|---|---|
| 基准任务数 | ≥ `MIN_SAMPLES`(8) | 少于 8 个的话,一个任务的偶然波动就能翻转结论 |
| 修复轮次 | 平均降低 ≥ `ROUNDS_DELTA`(0.3 轮) | 8 个任务里有 3 个少走一轮,就是实打实的收益 |
| 门禁一次通过率 | 提升 ≥ `PASS_RATE_DELTA`(0.10) | 低于 10 个百分点在这个样本量下与噪声无法区分 |

**两条指标满足其一即算有效**,不要求同时满足:有的手艺缩短修复循环(failure-diagnosis),
有的提高一次成功率(output-discipline),它们改善的本来就不是同一件事。

## 不达标怎么办

不达标**不等于这份手艺没用**,可能是基准任务集选错了——比如用一批纯 CRUD 任务去测
`codebase-survey`,它本来就没有发挥空间。所以报告要同时输出两件事:指标差,以及
基准任务的构成。判断"是内容问题还是基准问题"是人的活,这里只负责把证据摆齐。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from agentgenome.genome import craft

#: 基准任务数下限。低于它,一个任务的偶然波动就能翻转结论。
MIN_SAMPLES = 8

#: 修复轮次的平均降幅门槛。
ROUNDS_DELTA = 0.3

#: 门禁一次通过率的提升门槛。低于 10 个百分点在这个样本量下与噪声无法区分。
PASS_RATE_DELTA = 0.10

#: 首批六项 P0 手艺。它们是这套基准的第一批被测对象。
P0_CRAFTS = (
    "genome-navigation",
    "codebase-survey",
    "map-authoring",
    "failure-diagnosis",
    "output-discipline",
    "log-forensics",
)

COMMON_ROOT = Path(__file__).resolve().parents[2] / "genome" / "procedures" / "_common" / "craft"


@dataclass(frozen=True)
class Arm:
    """A/B 一侧的观测结果。"""

    samples: int
    total_rounds: int
    first_pass: int

    @property
    def mean_rounds(self) -> float:
        return self.total_rounds / self.samples if self.samples else 0.0

    @property
    def pass_rate(self) -> float:
        return self.first_pass / self.samples if self.samples else 0.0


@dataclass(frozen=True)
class Verdict:
    """一次 A/B 的结论。"""

    craft_name: str
    without: Arm
    with_: Arm

    @property
    def rounds_gain(self) -> float:
        """修复轮次降了多少。正数是好事。"""
        return self.without.mean_rounds - self.with_.mean_rounds

    @property
    def pass_rate_gain(self) -> float:
        return self.with_.pass_rate - self.without.pass_rate

    @property
    def enough_samples(self) -> bool:
        return min(self.without.samples, self.with_.samples) >= MIN_SAMPLES

    @property
    def effective(self) -> bool:
        """两条指标满足其一即算有效——它们改善的本来就不是同一件事。"""
        if not self.enough_samples:
            return False
        return self.rounds_gain >= ROUNDS_DELTA or self.pass_rate_gain >= PASS_RATE_DELTA

    def render(self) -> str:
        lines = [
            f"## {self.craft_name}",
            f"样本:挂 {self.with_.samples} / 不挂 {self.without.samples}"
            f"(下限 {MIN_SAMPLES}){'' if self.enough_samples else ' —— **样本不足**'}",
            f"修复轮次:{self.without.mean_rounds:.2f} → {self.with_.mean_rounds:.2f}"
            f"(降 {self.rounds_gain:+.2f},门槛 {ROUNDS_DELTA})",
            f"一次通过率:{self.without.pass_rate:.0%} → {self.with_.pass_rate:.0%}"
            f"(升 {self.pass_rate_gain:+.0%},门槛 {PASS_RATE_DELTA:.0%})",
            f"结论:{'**有效**' if self.effective else '未达标 —— 要判断是内容问题还是基准问题'}",
        ]
        return "\n".join(lines)


def judge(craft_name: str, without: Arm, with_: Arm) -> Verdict:
    return Verdict(craft_name=craft_name, without=without, with_=with_)


# --- 这些不跑 Agent,常规 CI 里就能跑 -------------------------------------------


class TestThresholds:
    """阈值逻辑本身是纯函数,该被常规测试盯住——否则门槛写错了没人发现。"""

    def test_a_clear_win_on_rounds_counts(self) -> None:
        verdict = judge("x", Arm(10, 25, 4), Arm(10, 19, 5))

        assert verdict.rounds_gain == pytest.approx(0.6)
        assert verdict.effective

    def test_a_clear_win_on_pass_rate_counts(self) -> None:
        verdict = judge("x", Arm(10, 20, 4), Arm(10, 20, 6))

        assert verdict.pass_rate_gain == pytest.approx(0.2)
        assert verdict.effective

    def test_a_marginal_gain_does_not_count(self) -> None:
        """低于门槛的改善与噪声无法区分。放它过去,门就形同虚设。"""
        verdict = judge("x", Arm(10, 20, 5), Arm(10, 19, 5))

        assert verdict.rounds_gain == pytest.approx(0.1)
        assert not verdict.effective

    def test_too_few_samples_never_counts_however_good_it_looks(self) -> None:
        """3 个任务里赢 2 个不说明任何问题。"""
        verdict = judge("x", Arm(3, 9, 0), Arm(3, 3, 3))

        assert verdict.rounds_gain == pytest.approx(2.0)
        assert not verdict.effective

    def test_a_regression_is_not_effective(self) -> None:
        verdict = judge("x", Arm(10, 19, 6), Arm(10, 25, 4))

        assert not verdict.effective

    def test_the_report_shows_the_evidence_not_just_the_verdict(self) -> None:
        """不达标可能是基准选错了,不一定是内容差——所以两者都要摆出来。"""
        rendered = judge("x", Arm(3, 9, 0), Arm(3, 3, 3)).render()

        assert "样本不足" in rendered
        assert "修复轮次" in rendered and "一次通过率" in rendered


class TestLibrary:
    """手艺库本身的形态。这些不需要跑 Agent。"""

    @pytest.mark.parametrize("name", P0_CRAFTS)
    def test_every_p0_craft_exists(self, name: str) -> None:
        assert (COMMON_ROOT / name / craft.CRAFT_MANIFEST).is_file()

    @pytest.mark.parametrize("name", P0_CRAFTS)
    def test_every_p0_craft_fits_the_budget(self, name: str) -> None:
        lines = (COMMON_ROOT / name / craft.CRAFT_MANIFEST).read_text(encoding="utf-8").count("\n")
        assert lines <= craft.LINE_BUDGET, f"{name} 有 {lines} 行,超出 {craft.LINE_BUDGET}"

    @pytest.mark.parametrize("name", P0_CRAFTS)
    def test_every_p0_craft_carries_counter_examples(self, name: str) -> None:
        """只说"该怎么做"的手艺没有判别力——员工分不清自己做的算不算数。"""
        body = (COMMON_ROOT / name / craft.CRAFT_MANIFEST).read_text(encoding="utf-8")
        assert "反例" in body, f"{name} 没有反例段落"
        assert "❌" in body, f"{name} 的反例段落没有具体反例"

    @pytest.mark.parametrize("name", P0_CRAFTS)
    def test_every_p0_craft_has_a_self_check(self, name: str) -> None:
        body = (COMMON_ROOT / name / craft.CRAFT_MANIFEST).read_text(encoding="utf-8")
        assert "自检" in body and "- [ ]" in body, f"{name} 没有自检清单"

    @pytest.mark.parametrize("name", P0_CRAFTS)
    def test_no_craft_restates_the_contract(self, name: str) -> None:
        """输入输出定义在 procedure.yaml 里。两处各写一遍必然发散。"""
        body = (COMMON_ROOT / name / craft.CRAFT_MANIFEST).read_text(encoding="utf-8")
        assert "inputs:" not in body and "outputs:" not in body

    def test_the_writing_guide_ships_with_the_library(self) -> None:
        assert (COMMON_ROOT / "README.md").is_file()


# --- 这一条要跑真实 Agent,默认跳过 --------------------------------------------


@pytest.mark.craft_ab
@pytest.mark.skip(reason="要跑真实 Agent。手工触发:pytest -m craft_ab --no-skip")
def test_p0_crafts_measured_against_the_baseline_suite() -> None:
    """跑基准任务集,对每份 P0 手艺出一份 A/B 报告。

    **不要求全部达标。** 不达标的要在报告里写明是内容问题还是基准问题——判断是人的活,
    这里只负责把证据摆齐。
    """
    pytest.fail("基准任务集尚未录制。见 tests/fixtures/ 下的录制库与本文件顶部的阈值说明。")
