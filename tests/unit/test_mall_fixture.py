"""夹具仓自身的验收：它必须是真实的 git 仓，否则后面所有测试都建在沙上。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agentgenome import paths
from agentgenome.genome.features import features_covering
from agentgenome.genome.loader import load_tree
from agentgenome.genome.models import ProjectMap
from agentgenome.genome.tree import write_tree
from tests.fixtures.mall import (
    INVARIANT_CARDS,
    MALL_MODULES,
    install_invariant_cards,
    materialize_mall,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_materializes_both_modules_as_real_git_repos(tmp_path: Path) -> None:
    repos = materialize_mall(tmp_path / "src")

    assert set(repos) == set(MALL_MODULES)
    for repo in repos.values():
        assert (repo.worktree / ".git").is_dir()
        assert _git(repo.worktree, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_each_module_has_a_bare_remote_carrying_the_initial_commit(tmp_path: Path) -> None:
    repos = materialize_mall(tmp_path / "src")

    for repo in repos.values():
        assert _git(repo.remote, "rev-parse", "--is-bare-repository") == "true"
        assert _git(repo.remote, "rev-parse", "main") == _git(repo.worktree, "rev-parse", "main")


def test_mall_carries_a_cross_module_contract_and_a_migrations_dir(tmp_path: Path) -> None:
    """契约文件与迁移目录是后续影响判定与风险评级的素材，夹具必须带上。"""
    repos = materialize_mall(tmp_path / "src")

    inventory = repos["inventory-service"].worktree
    order = repos["order-service"].worktree

    assert (inventory / "api" / "reserve.yaml").is_file()
    assert (order / "migrations").is_dir()


def test_mall_carries_a_deliberately_failing_test(tmp_path: Path) -> None:
    """后续 PRD 需要一个"会失败的测试"来演示修复循环，夹具预置它。"""
    repos = materialize_mall(tmp_path / "src")
    order = repos["order-service"].worktree

    assert (order / "tests" / "test_known_failure.py").is_file()


def test_mall_carries_an_attack_target_that_current_tests_do_not_catch(tmp_path: Path) -> None:
    """对抗 QA 的靶子:**现有测试全绿,但边界上一触即溃。**

    这条测试自己就是"门禁拦不住脆的"这句话的证明——它先跑一遍模块自己的测试(全绿),
    再拿一个边界输入把同一份实现打穿。两件事同时成立,才配当红队的靶子。
    """
    repos = materialize_mall(tmp_path / "src")
    inventory = repos["inventory-service"].worktree

    green = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=inventory,
        capture_output=True,
        text=True,
    )
    assert green.returncode == 0, green.stdout + green.stderr

    sys.path.insert(0, str(inventory / "src"))
    try:
        from inventory.app import InventoryService  # noqa: PLC0415 —— 靶子是物化出来的那一份

        service = InventoryService(stock={"sku-1": 10, "sku-2": 5})
        with pytest.raises(IndexError):
            service.reserve_batch(["sku-1", "sku-2"], [3], order_id="ord-1")
        # 这才是真正的伤口:异常抛出去了,而库存已经扣了一半。
        assert service.stock == {"sku-1": 7, "sku-2": 5}
    finally:
        sys.path.remove(str(inventory / "src"))
        sys.modules.pop("inventory.app", None)
        sys.modules.pop("inventory", None)


def test_the_invariant_cards_are_routable_knowledge(tmp_path: Path) -> None:
    """攻击清单要能被路由命中。命不中的卡片对红队没有意义——它拿不到那份清单。"""
    root = tmp_path / "ws"
    (root / paths.KNOWLEDGE).mkdir(parents=True)
    # 覆盖路径要指向真的存在的代码,否则知识树会判它是一条悬空引用——而那正是对的:
    # 一张指着不存在的目录的卡片,路由永远命不中它。
    (root / "repos/order-service/src/order").mkdir(parents=True)
    (root / "repos/inventory-service/src/inventory").mkdir(parents=True)
    write_tree(
        root,
        ProjectMap.model_validate(
            {
                "version": 1,
                "project": {"name": "mall"},
                "modules": [
                    {"id": module_id, "path": f"repos/{module_id}/"} for module_id in MALL_MODULES
                ],
            }
        ),
    )

    written = install_invariant_cards(root)

    assert len(written) == 2
    tree = load_tree(root)
    for module_id, card in INVARIANT_CARDS.items():
        loaded = tree.card(module_id, card["id"])
        assert loaded is not None, f"{module_id} 的卡片没被知识树读出来"
        assert "## 不变量" in loaded.body
        hit = features_covering(tree, card["scope"].replace("**", "app.py"))
        assert [ref.feature.id for ref in hit] == [card["id"]]
