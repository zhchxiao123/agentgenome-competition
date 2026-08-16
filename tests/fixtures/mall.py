"""`examples/mall` 双模块示例仓的物化。

示例仓的内容以普通文件形式存放在 `examples/mall/` 下（不含 `.git`，避免嵌套仓库
把工具链搞乱）。测试开始时由这里的 helper 复制到 tmp 目录并 `git init`，同时建出
bare 远端——于是测试跑的是真实 git 仓上的真实 git 命令。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from tests.fixtures.git import IDENTITY
from tests.fixtures.git import git as _git

EXAMPLES_ROOT = Path(__file__).resolve().parents[2] / "examples" / "mall"

#: 示例仓中的业务模块，与 `examples/mall/` 下的目录同名。
MALL_MODULES = ("order-service", "inventory-service")


@dataclass(frozen=True)
class MaterializedRepo:
    """一个已物化的业务仓：工作树 + 它的 bare 远端。"""

    name: str
    worktree: Path
    remote: Path

    @property
    def remote_url(self) -> str:
        return str(self.remote)


def materialize_repo(name: str, dest_root: Path) -> MaterializedRepo:
    """把 `examples/mall/<name>` 物化成一个带 bare 远端的真实 git 仓。"""
    source = EXAMPLES_ROOT / name
    if not source.is_dir():
        raise FileNotFoundError(f"示例仓不存在: {source}")

    worktree = dest_root / name
    # 排除 __pycache__:仓库根的 .gitignore 管不到物化出来的新仓,不排的话 .pyc 会被
    # `git add -A` 跟踪,之后任何一次 pytest 都让这个仓变脏——而变脏会被越权检查
    # 判成"这个员工动了业务代码"。
    # 示例仓可能被本地跑过，虚拟环境既不是 fixture 内容，内部链接也不一定可复制。
    shutil.copytree(
        source,
        worktree,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv"),
    )
    # 真实的 Python 仓都有这一条。少了它,门禁跑一次测试就把工作树弄脏。
    (worktree / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")

    remote = dest_root / f"{name}.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )

    _git(worktree, "init", "--initial-branch=main")
    _git(worktree, "add", "-A")
    _git(worktree, *IDENTITY, "commit", "-m", f"chore: 初始化 {name}")
    _git(worktree, "remote", "add", "origin", str(remote))
    _git(worktree, "push", "-u", "origin", "main")

    return MaterializedRepo(name=name, worktree=worktree, remote=remote)


def materialize_mall(dest_root: Path) -> dict[str, MaterializedRepo]:
    """物化整个示例仓，返回 `模块名 -> 已物化仓` 的映射。"""
    dest_root.mkdir(parents=True, exist_ok=True)
    return {name: materialize_repo(name, dest_root) for name in MALL_MODULES}


#: 示例仓的不变量素材:每个模块一张功能卡片,正文里写着**可判定**的不变量。
#:
#: **对抗那条线的输入就是这些卡片。** 不变量卡不是一种新的卡片类型——它就是功能卡片正文里
#: 的一个小节,而"哪条线碰不得"这份清单本来就该住在那儿。攻击就是逐条问"真的碰不得吗"。
#:
#: 每条不变量写成一句能被判定的话。"要注意并发"这种没法验的说法进了卡片,红队照着它构造
#: 不出任何攻击,而它看起来又像是写了。
INVARIANT_CARDS: dict[str, dict[str, str]] = {
    "inventory-service": {
        "id": "reserve-stock",
        "scope": "repos/inventory-service/src/inventory/**",
        "summary": "库存预占:单个与批量,以及它们的原子性约束",
        "body": """\
## 时序

`reserve` 先查可用量再扣减,扣减与预占记录在同一步写下。`reserve_batch` 按 sku 逐个调
`reserve`,给每个 sku 派生一个 `{order_id}-{index}` 的预占 id。

## 不变量

1. 任何时刻 `stock[sku]` 不得为负。
2. 可用量不足时必须抛 `OutOfStock`,不得部分预占。
3. **批量预占是原子的:任何一项失败,整批都不生效**——半成功的批次会让库存凭空消失,
   而调用方拿到的是一个异常,它没有任何办法知道哪几项已经扣掉了。
4. 预占 id 在一次批量里不得重复。

## 测试要点

批量路径只有happy path 有用例覆盖。第 3 条不变量**没有任何测试碰过**。
""",
    },
    "order-service": {
        "id": "place-order",
        "scope": "repos/order-service/src/order/**",
        "summary": "下单:先向库存域预占,成功才落库",
        "body": """\
## 时序

`place` 先查重,再调库存域的预占接口,拿到预占 id 之后才把订单记下来。

## 不变量

1. 同一个 `order_id` 只能落库一次。
2. **预占失败时订单不得落库**——落了库而没有库存,发货时才会发现,那时订单已经对客户承诺过了。
3. 订单表里的预占 id 必须是库存域真的发出过的那一个。

## 测试要点

第 1、2 条各有用例。第 3 条依赖跨模块契约,单测覆盖不到,靠集成测试。
""",
    },
}


def install_invariant_cards(workspace_root: Path) -> list[Path]:
    """把不变量素材装进一个已 init 的 Workspace 的知识树。**返回写下的卡片路径。**

    卡片住在 Workspace 的知识树里,不住业务仓——它是"我们对这个项目的认知",不是项目自己的
    文件。这也是它必须由夹具装进去而不是跟着示例仓复制过来的原因。
    """
    from agentgenome import paths
    from agentgenome.genome.tree import MODULE_MAP

    written: list[Path] = []
    for module_id, card in INVARIANT_CARDS.items():
        module_dir = workspace_root / paths.MODULES / module_id
        target = module_dir / "features" / f"{card['id']}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\n"
            f"id: {card['id']}\n"
            f"scope: [\"{card['scope']}\"]\n"
            f"summary: \"{card['summary']}\"\n"
            "confidence: high\n"
            "---\n" + card["body"],
            encoding="utf-8",
        )
        written.append(target)

        map_path = module_dir / MODULE_MAP
        payload = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
        payload["features"] = [
            {
                "id": card["id"],
                "summary": card["summary"],
                "scope": [card["scope"]],
                "card": f"features/{card['id']}.md",
            }
        ]
        map_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    return written
