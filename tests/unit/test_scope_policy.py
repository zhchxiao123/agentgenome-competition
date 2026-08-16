"""授权范围的判定:路径进,越权清单出。

这一层是纯函数,所以可以把危险的边界逐条钉死——**权限检查里的过度匹配会放行越权**,
是全仓最不该出错的地方。
"""

from __future__ import annotations

import pytest

from agentgenome.core.scope import ScopePolicy, ViolationKind, compile_glob


def _policy(write: list[str] | None = None, forbid: list[str] | None = None) -> ScopePolicy:
    return ScopePolicy(write_paths=tuple(write or []), forbid_paths=tuple(forbid or []))


# --- glob 语义 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        # `*` 不跨目录——fnmatch 会在这里出错,而出错的方向是放行。
        ("genome/*", "genome/rules/architecture.md", False),
        ("genome/*", "genome/notes.md", True),
        # `**` 跨目录
        ("repos/**", "repos/order-service/src/app.py", True),
        ("repos/**", "repos/order-service/a/b/c/deep.py", True),
        # `a/**` 覆盖 a 自身——不然"禁写这个目录"会漏掉目录本身被删或被换成文件
        ("repos/**", "repos", True),
        ("genome/rules/**", "genome/rules", True),
        ("genome/rules/**", "genome/rules/architecture.md", True),
        ("genome/rules/**", "genome/knowledge/map.yaml", False),
        # 全量通配
        ("**", "anything/at/all.py", True),
        # 前缀相近但不同的目录不该被误伤
        ("repos/api/**", "repos/api-2/app.py", False),
        ("tasks/ag-1/**", "tasks/ag-11/notes.md", False),
        # 中间段的 `**`
        ("**/migrations/**", "repos/order-service/db/migrations/001.sql", True),
        ("**/migrations/**", "repos/order-service/db/models.py", False),
    ],
)
def test_glob_semantics(glob: str, path: str, expected: bool) -> None:
    assert bool(compile_glob(glob).match(path)) is expected


# --- 判定 -------------------------------------------------------------------


def test_a_path_inside_the_write_globs_is_allowed() -> None:
    assert _policy(["repos/order-service/**"]).allows("repos/order-service/src/app.py") is True


def test_a_path_outside_the_write_globs_is_a_violation() -> None:
    violations = _policy(["repos/order-service/**"]).violations(
        ["repos/inventory-service/src/app.py"]
    )

    assert [v.kind for v in violations] == [ViolationKind.OUTSIDE]
    assert violations[0].path == "repos/inventory-service/src/app.py"


def test_forbid_wins_over_write() -> None:
    """反过来的话,一条宽泛的 write_paths 会悄悄把禁止规则吃掉。"""
    policy = _policy(["**"], ["genome/rules/**"])

    violations = policy.violations(["genome/rules/architecture.md"])

    assert [v.kind for v in violations] == [ViolationKind.FORBIDDEN]
    assert violations[0].rule == "genome/rules/**"


def test_an_empty_write_list_forbids_everything() -> None:
    """默认值必须站在安全的那一侧:写了一半的配置不该拥有完全权限。"""
    assert _policy().violations(["anything.py"])


def test_a_compliant_change_set_produces_no_violations() -> None:
    """正向用例——只测拦得住不测放得过,等于没测。"""
    policy = _policy(["repos/order-service/**", "tasks/ag-1/**"], ["genome/rules/**"])

    assert policy.violations(["repos/order-service/src/app.py", "tasks/ag-1/notes.md"]) == []


def test_the_report_says_which_rule_was_crossed() -> None:
    """ "越了哪条界"要能直接读出来,不然人得自己拿 glob 逐条比。"""
    policy = _policy(["**"], [".github/**"])

    violation = policy.violations([".github/workflows/ci.yml"])[0]

    assert violation.rule == ".github/**"
    assert ".github/workflows/ci.yml" in violation.render()


def test_violations_are_ordered_by_path() -> None:
    """报告要稳定,否则两次运行的 diff 全是噪声。"""
    policy = _policy([])

    violations = policy.violations(["z.py", "a.py", "m.py"])

    assert [v.path for v in violations] == ["a.py", "m.py", "z.py"]


# --- 受保护路径叠加 ---------------------------------------------------------


def test_protected_paths_stack_on_top_of_the_employee_rules() -> None:
    """任何员工触碰受保护路径都判越权,不管它自己的 forbid 写了什么。"""
    policy = _policy(["**"]).with_protected(["genome/rules/**"])

    assert policy.violations(["genome/rules/architecture.md"])


def test_stacking_keeps_the_employee_own_forbid_rules() -> None:
    policy = _policy(["**"], [".github/**"]).with_protected(["genome/rules/**"])

    assert policy.violations([".github/ci.yml"])
    assert policy.violations(["genome/rules/x.md"])


def test_stacking_does_not_mutate_the_original() -> None:
    """策略是不可变的:一个 Job 的叠加不该渗到下一个。"""
    policy = _policy(["**"])

    policy.with_protected(["genome/rules/**"])

    assert policy.violations(["genome/rules/x.md"]) == []


# --- 路径归一化 -------------------------------------------------------------


def test_a_leading_dot_is_not_eaten() -> None:
    """把开头的点当成 `./` 削掉,会让 `.github/**` 变成 `github/**` 而放行越权。"""
    assert _policy(["**"], [".github/**"]).violations([".github/workflows/ci.yml"])


def test_a_relative_prefix_does_not_change_the_verdict() -> None:
    assert _policy(["repos/order-service/**"]).violations(["./repos/order-service/app.py"]) == []
