"""① 确定性扫描:能算出来的事不花 token。

目录树、语言、构建文件、依赖清单、变更热区——**全是脚本能算的**。同一件事,脚本几秒钟、
零成本;Agent 几分钟、真金白银。

热区的用途只有两个:给模块划分做依据,给深读排优先序。**它不决定哪些功能点建卡片**——
ADR-0003 已经否掉了热区优先建卡。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentgenome.genome.scan import MountState, scan_workspace
from tests.fixtures.git import fake_checkout

IDENTITY = (
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@example.com",
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _declare_submodules(root: Path, *paths: str) -> None:
    """写一份 `.gitmodules`。

    候选来源是它,不是目录树——所以夹具里必须有它,否则扫出来的是空。
    """
    root.joinpath(".gitmodules").write_text(
        "".join(
            f'[submodule "{path}"]\n\tpath = {path}\n\turl = ../{path}.git\n' for path in paths
        ),
        encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _init = tmp_path / "ws"
    _init.mkdir()
    subprocess.run(
        ["git", "-C", str(_init), "init", "-q", "--initial-branch=main"],
        check=True,
        capture_output=True,
    )
    (_init / "repos/order-service" / "src").mkdir(parents=True)
    (_init / "repos/order-service" / "pyproject.toml").write_text(
        '[project]\nname = "order"\ndependencies = ["httpx", "pydantic"]\n', encoding="utf-8"
    )
    (_init / "repos/order-service" / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    (_init / "repos/inventory-service").mkdir(parents=True, exist_ok=True)
    (_init / "repos/inventory-service" / "package.json").write_text(
        '{"name": "web", "dependencies": {"react": "18"}}', encoding="utf-8"
    )
    fake_checkout(_init, "repos/order-service", "repos/inventory-service")
    _declare_submodules(_init, "repos/order-service", "repos/inventory-service")
    _git(_init, "add", "-A")
    subprocess.run(
        ["git", "-C", str(_init), *IDENTITY, "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return _init


# --- 不烧 token ---------------------------------------------------------------


def test_the_scan_touches_no_agent(repo: Path) -> None:
    """**这一阶段存在的全部理由。** 一秒钟的事不该花一块钱。"""
    import inspect

    from agentgenome.genome import scan

    source = inspect.getsource(scan)

    for forbidden in ("dispatch_procedure", "AgentPool", "run_job"):
        assert forbidden not in source, f"扫描里出现了 {forbidden}"


# --- 语言与构建文件 -----------------------------------------------------------


def test_it_recognises_a_python_module(repo: Path) -> None:
    found = {item.path: item for item in scan_workspace(repo).candidates}

    assert found["repos/order-service"].language == "python"
    assert "pyproject.toml" in found["repos/order-service"].build_files


def test_it_recognises_a_node_module(repo: Path) -> None:
    found = {item.path: item for item in scan_workspace(repo).candidates}

    assert found["repos/inventory-service"].language == "node"


def test_it_parses_the_dependency_manifest(repo: Path) -> None:
    found = {item.path: item for item in scan_workspace(repo).candidates}

    assert "httpx" in found["repos/order-service"].dependencies
    assert "react" in found["repos/inventory-service"].dependencies


def test_a_directory_that_is_not_a_mounted_repo_is_not_a_candidate(repo: Path) -> None:
    """一个放图片的目录不是模块。把它当候选只会让人在闸门上多划掉一行。

    判据是"有没有被挂载",不是"有没有构建文件"——见下一条。
    """
    (repo / "assets").mkdir()
    (repo / "assets" / "logo.png").write_bytes(b"x")

    assert "assets" not in {item.path for item in scan_workspace(repo).candidates}


# --- 候选来自挂载声明,不是目录树 -----------------------------------------------


def test_a_mounted_repo_with_no_build_file_is_still_a_candidate(repo: Path) -> None:
    """**"这个仓算不算一个模块"是闸门上该由人判的事。**

    此前的判据是"目录里有没有可识别的构建文件",于是纯配置仓、文档仓被静默丢弃——
    而人看不到被悄悄扔掉的东西,闸门存在的全部理由就落空了。
    """
    fake_checkout(repo, "repos/billing-service")
    (repo / "repos/billing-service" / "README.md").write_text("# 纯文档仓\n", encoding="utf-8")
    _declare_submodules(
        repo, "repos/order-service", "repos/inventory-service", "repos/billing-service"
    )

    found = {item.path: item for item in scan_workspace(repo).candidates}

    assert "repos/billing-service" in found
    assert found["repos/billing-service"].language is None
    assert found["repos/billing-service"].build_files == ()


def test_candidates_come_from_the_mount_declaration_not_the_directory_tree(repo: Path) -> None:
    """挂在哪儿都找得到——这条正是"业务仓是根的一级子目录"那个假设的替代品。

    用一个**刻意不常规**的挂载深度:发现逻辑一旦重新长出对目录布局的假设,这条会红,
    而按标准 `repos/<仓>/` 布局写的用例不会。
    """
    (repo / "vendor" / "deep" / "legacy-billing").mkdir(parents=True)
    fake_checkout(repo, "vendor/deep/legacy-billing")
    (repo / "vendor" / "deep" / "legacy-billing" / "pyproject.toml").write_text(
        '[project]\nname = "billing"\ndependencies = ["httpx"]\n', encoding="utf-8"
    )
    _declare_submodules(repo, "vendor/deep/legacy-billing")

    found = {item.path: item for item in scan_workspace(repo).candidates}

    assert set(found) == {"vendor/deep/legacy-billing"}
    assert found["vendor/deep/legacy-billing"].language == "python"
    assert "httpx" in found["vendor/deep/legacy-billing"].dependencies


def test_a_workspace_with_no_mounted_repos_scans_to_an_empty_candidate_list(
    tmp_path: Path,
) -> None:
    """没有 `.gitmodules` 不是错误:一个还没挂任何业务仓的 Workspace 照样要能扫。"""
    bare = tmp_path / "empty"
    bare.mkdir()
    subprocess.run(
        ["git", "-C", str(bare), "init", "-q", "--initial-branch=main"],
        check=True,
        capture_output=True,
    )

    assert scan_workspace(bare).candidates == ()


# --- 挂载点三态 ---------------------------------------------------------------
#
# 一个"空"的挂载点可以是三件完全不同的事,而它们要求的动作正好相反:
# 未就绪 → 修环境;空 → 继续干活;有内容 → 做判断。合并任意两个,闸门就在问一个人答不了的问题。


def test_a_declared_mount_point_that_is_not_on_disk_is_unready(repo: Path) -> None:
    """**不跳过。** 声明过的仓一个都不能从闸门上消失——人看不到被悄悄扔掉的东西。"""
    _declare_submodules(
        repo, "repos/order-service", "repos/inventory-service", "repos/ghost-service"
    )

    found = {item.path: item for item in scan_workspace(repo).candidates}

    assert found["repos/ghost-service"].state is MountState.UNREADY


def test_a_mount_point_without_a_git_entry_is_unready(repo: Path) -> None:
    """最常见的那一种:clone 时忘了 `--recurse-submodules`。

    **git 会为子模块建空目录占位**,所以"目录在不在"完全判不出这件事——判据必须是 `.git`。
    """
    (repo / "repos/billing-service").mkdir(parents=True)
    _declare_submodules(
        repo, "repos/order-service", "repos/inventory-service", "repos/billing-service"
    )

    found = {item.path: item for item in scan_workspace(repo).candidates}

    assert found["repos/billing-service"].state is MountState.UNREADY


def test_a_checked_out_repo_with_nothing_in_it_is_empty(repo: Path) -> None:
    """绿地新仓:已经 checkout 了,只是代码还没开始写。**这是正常状态,不是错误。**"""
    fake_checkout(repo, "repos/greenfield")
    _declare_submodules(repo, "repos/order-service", "repos/inventory-service", "repos/greenfield")

    found = {item.path: item for item in scan_workspace(repo).candidates}

    assert found["repos/greenfield"].state is MountState.EMPTY


def test_a_checked_out_repo_with_files_is_populated(repo: Path) -> None:
    """没有可识别构建文件不影响状态判定——那只决定语言那一栏填不填得出。"""
    fake_checkout(repo, "repos/docs-site")
    (repo / "repos/docs-site" / "README.md").write_text("# 纯文档仓\n", encoding="utf-8")
    _declare_submodules(repo, "repos/order-service", "repos/inventory-service", "repos/docs-site")

    found = {item.path: item for item in scan_workspace(repo).candidates}

    assert found["repos/docs-site"].state is MountState.POPULATED
    assert found["repos/docs-site"].language is None


def test_a_normal_repo_is_populated(repo: Path) -> None:
    found = {item.path: item for item in scan_workspace(repo).candidates}

    assert found["repos/order-service"].state is MountState.POPULATED


# --- 变更热区 ----------------------------------------------------------------


def test_hot_paths_are_ranked_by_how_often_they_change(repo: Path) -> None:
    for index in range(3):
        (repo / "repos/order-service" / "src" / "app.py").write_text(
            f"print({index})\n", encoding="utf-8"
        )
        _git(repo, "add", "-A")
        subprocess.run(
            ["git", "-C", str(repo), *IDENTITY, "commit", "-q", "-m", f"c{index}"],
            check=True,
            capture_output=True,
        )

    hot = scan_workspace(repo).hot_paths

    assert hot
    assert hot[0].path.startswith("repos/order-service")
    assert hot[0].changes >= 3


def test_a_repository_with_no_history_gives_an_empty_hot_list(tmp_path: Path) -> None:
    """空仓不该炸——一个刚 init 出来的 Workspace 照样要能扫。"""
    bare = tmp_path / "bare"
    bare.mkdir()

    assert scan_workspace(bare).hot_paths == ()


def test_the_window_can_be_narrowed(repo: Path) -> None:
    assert scan_workspace(repo, since_days=1).hot_paths is not None


# --- 可复现与落盘 -------------------------------------------------------------


def test_two_scans_agree(repo: Path) -> None:
    """确定性的意思就是这个。"""
    assert scan_workspace(repo).as_dict() == scan_workspace(repo).as_dict()


def test_the_result_is_readable_by_a_human(repo: Path) -> None:
    payload = scan_workspace(repo).as_dict()

    # 按挂载路径排序。候选来自一个**集合**(`.gitmodules` 的声明),所以"给出顺序"这个
    # 概念不存在;要紧的是排序确定,见上一条。
    assert [item["path"] for item in payload["candidates"]] == [
        "repos/inventory-service",
        "repos/order-service",
    ]
    assert "hot_paths" in payload


def test_the_window_is_a_configuration_knob() -> None:
    """**光有参数不算数。** 前几轮评审三次命中「配置项没人读」这个形状。"""
    from agentgenome.config import Config
    from agentgenome.genome.scan import DEFAULT_SINCE_DAYS

    assert Config().genome_tasks.hot_path_since_days == DEFAULT_SINCE_DAYS
