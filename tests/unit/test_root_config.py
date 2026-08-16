"""根配置 agentgenome.yaml 的加载与校验。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agentgenome.config import (
    INITIAL_ROUND_LIMIT,
    INITIAL_TIMEOUT_SECONDS,
    INITIAL_TOKEN_LIMIT,
    INITIAL_TURN_LIMIT,
    load_config,
    render_default_config,
)
from agentgenome.genome.loader import GenomeValidationError

FULL = """\
runtime:
  default: claude-code
  claude-code: {cmd: claude, max_turns: 40}
concurrency: {global_jobs: 3}
budgets: {per_task_tokens: 1500000, per_job_tokens: 300000}
limits: {max_fix_rounds: 3, job_timeout_s: 1800}
approval:
  approvers: [xiao@example.com]
  notify: {webhook: "https://open.feishu.cn/hook"}
platform:
  git_host: github
  protected_branch: main
umodel: {enabled: false, gateway: "http://localhost:8080", workspace: mall}
"""


def _write(tmp_path: Path, config: str) -> Path:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    (root / "agentgenome.yaml").write_text(textwrap.dedent(config))
    return root


def test_loads_a_full_config(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, FULL))

    assert config.runtime.default == "claude-code"
    assert config.runtime.runtimes["claude-code"].cmd == "claude"
    assert config.concurrency.global_jobs == 3
    assert config.budgets.per_task_tokens == 1_500_000
    assert config.limits.max_fix_rounds == 3
    assert config.approval.approvers == ["xiao@example.com"]
    assert config.platform.git_host == "github"
    assert config.umodel.enabled is False


def test_every_section_has_a_usable_default(tmp_path: Path) -> None:
    """新 Workspace 的配置可以几乎是空的,默认值必须能直接跑起来。"""
    config = load_config(_write(tmp_path, "{}\n"))

    assert config.concurrency.global_jobs > 0
    assert config.limits.max_fix_rounds > 0
    assert config.limits.job_timeout_s > 0
    assert config.platform.protected_branch == "main"
    assert config.approval.approvers == []
    assert config.budgets.enforce is False


def test_new_workspaces_start_with_generous_execution_limits(tmp_path: Path) -> None:
    """初始化不该用一组很快撞上的试用额度把员工卡住。"""
    config = load_config(_write(tmp_path, render_default_config()))

    assert config.runtime.runtimes["claude-code"].max_turns == INITIAL_TURN_LIMIT
    assert config.budgets.per_task_tokens == INITIAL_TOKEN_LIMIT
    assert config.budgets.per_job_tokens == INITIAL_TOKEN_LIMIT
    assert config.budgets.session_tokens == INITIAL_TOKEN_LIMIT
    assert config.limits.max_fix_rounds == INITIAL_ROUND_LIMIT
    assert config.limits.job_timeout_s == INITIAL_TIMEOUT_SECONDS
    assert config.genome_tasks.per_task_tokens == INITIAL_TOKEN_LIMIT
    assert config.topology.critique.max_rounds == INITIAL_ROUND_LIMIT
    assert config.topology.best_of_n.max_attempts == INITIAL_ROUND_LIMIT


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(GenomeValidationError) as excinfo:
        load_config(_write(tmp_path, "concurrancy: {global_jobs: 3}\n"))

    assert "concurrancy" in excinfo.value.render()


def test_unknown_nested_field_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(GenomeValidationError) as excinfo:
        load_config(_write(tmp_path, "limits: {max_fix_round: 3}\n"))

    assert "max_fix_round" in excinfo.value.render()


def test_missing_config_is_a_readable_error(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    with pytest.raises(GenomeValidationError) as excinfo:
        load_config(root)

    assert "agentgenome.yaml" in excinfo.value.render()


def test_unknown_default_runtime_is_rejected(tmp_path: Path) -> None:
    """默认运行时必须在 runtime 段里有对应配置,否则任务派发时才会炸。"""
    with pytest.raises(GenomeValidationError) as excinfo:
        load_config(_write(tmp_path, "runtime: {default: nope}\n"))

    assert "nope" in excinfo.value.render()


def test_git_host_must_be_a_known_platform(tmp_path: Path) -> None:
    with pytest.raises(GenomeValidationError) as excinfo:
        load_config(_write(tmp_path, "platform: {git_host: bitbucket}\n"))

    assert "bitbucket" in excinfo.value.render()
