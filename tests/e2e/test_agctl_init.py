"""`agctl init` 的端到端验收:走 CLI 入口,断言磁盘与 git 的最终状态。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentgenome.cli import app
from agentgenome.verification import load_verification_spec
from tests.fixtures.mall import materialize_mall
from tests.fixtures.tree import write_flat_as_tree

runner = CliRunner()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def mall(tmp_path: Path):
    return materialize_mall(tmp_path / "upstream")


def _init(workspace: Path, mall) -> None:
    result = runner.invoke(
        app,
        [
            "init",
            "--local-only",
            str(workspace),
            "--name",
            "example-mall",
            "--repo",
            mall["order-service"].remote_url,
            "--repo",
            mall["inventory-service"].remote_url,
        ],
    )
    assert result.exit_code == 0, result.output


def test_init_creates_a_git_workspace_with_the_standard_skeleton(tmp_path: Path, mall) -> None:
    workspace = tmp_path / "ws"

    _init(workspace, mall)

    assert (workspace / ".git").is_dir()
    for expected in (
        "agentgenome.yaml",
        "genome/knowledge/project-map.yaml",
        "genome/knowledge/modules",
        "genome/knowledge/lessons",
        "genome/rules",
        "genome/procedures",
        "employees/prompts",
        "scripts",
        ".gitignore",
        ".gitmodules",
    ):
        assert (workspace / expected).exists(), f"缺少 {expected}"


def test_init_configures_and_pushes_the_workspace_repository(tmp_path: Path, mall) -> None:
    workspace = tmp_path / "ws"
    remote = tmp_path / "workspace.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))

    result = runner.invoke(
        app,
        [
            "init",
            str(workspace),
            "--repo",
            mall["order-service"].remote_url,
            "--workspace-repo",
            str(remote),
        ],
    )

    assert result.exit_code == 0, result.output
    assert _git(workspace, "remote", "get-url", "origin") == str(remote)
    assert _git(remote, "show", "main:.gitmodules")


def test_init_requires_a_workspace_repository_unless_local_only(tmp_path: Path, mall) -> None:
    result = runner.invoke(
        app,
        ["init", str(tmp_path / "ws"), "--repo", mall["order-service"].remote_url],
    )

    assert result.exit_code != 0
    assert "必须提供 --workspace-repo" in result.output
    assert not (tmp_path / "ws").exists()


def test_skeleton_survives_a_fresh_clone(tmp_path: Path, mall) -> None:
    """git 不记录空目录。骨架目录必须带占位文件,否则 clone 出来是残缺的。"""
    workspace = tmp_path / "ws"
    _init(workspace, mall)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(workspace), str(clone)], check=True)

    for expected in (
        "genome/knowledge/modules",
        "genome/knowledge/lessons",
        "genome/knowledge/decisions",
        "genome/procedures",
        "employees/prompts",
        "scripts",
    ):
        assert (clone / expected).is_dir(), f"clone 后缺少 {expected}"


def test_the_mount_points_survive_a_fresh_clone(tmp_path: Path, mall) -> None:
    """挂载点在 clone 里必须是**声明齐全**的,哪怕子模块还没 checkout。

    嵌一层父目录之后这条不再是白测:`repos/` 本身不是任何一个子模块,它在克隆端是靠
    `.gitmodules` 里那两条路径推出来的。少了它们,新克隆的 Workspace 只是看起来正常——
    `git submodule update --init` 什么也拉不到,而症状要到派活时才显形。
    """
    workspace = tmp_path / "ws"
    _init(workspace, mall)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(workspace), str(clone)], check=True)

    declared = _git(clone, "config", "-f", ".gitmodules", "--get-regexp", r"submodule\..*\.path")
    assert "repos/order-service" in declared
    assert "repos/inventory-service" in declared


def test_init_mounts_business_repos_as_submodules(tmp_path: Path, mall) -> None:
    workspace = tmp_path / "ws"

    _init(workspace, mall)

    gitmodules = (workspace / ".gitmodules").read_text()
    assert "repos/order-service" in gitmodules
    assert "repos/inventory-service" in gitmodules
    assert (workspace / "repos" / "order-service" / ".git").exists()
    assert (workspace / "repos" / "inventory-service" / ".git").exists()


def test_the_mount_root_holds_nothing_but_the_business_repos(tmp_path: Path, mall) -> None:
    """`repos/` 是权限模型的支点(`repos/**` 就是"全部业务代码")。

    往里塞别的东西等于把它们一并授权给开发员工。
    """
    workspace = tmp_path / "ws"

    _init(workspace, mall)

    assert sorted(item.name for item in (workspace / "repos").iterdir()) == [
        "inventory-service",
        "order-service",
    ]


def test_the_mount_point_carries_the_repository_name(tmp_path: Path, mall) -> None:
    """挂载点本身就是身份线索。

    这是整条改动的理由:人和员工在 diff、失败报告、PR 里读到的每一条路径,都不必再回头
    查一次根索引才知道自己在看哪个仓。
    """
    workspace = tmp_path / "ws"

    _init(workspace, mall)

    assert (workspace / "repos" / "order-service" / "src").is_dir()


def test_the_mount_point_does_not_follow_an_upstream_rename(tmp_path: Path, mall) -> None:
    """挂载路径是**挂载时冻结的标签**,不是从远端地址算出来的派生值。

    不冻结的话,上游改一次名就会让知识卡片的覆盖范围、门禁配置、diff 基线集体失准——
    而那次改名发生在别人的仓库里,这边没有任何人会收到通知。
    """
    workspace = tmp_path / "ws"
    _init(workspace, mall)
    renamed = mall["order-service"].remote.parent / "orders-v2.git"
    mall["order-service"].remote.rename(renamed)
    _git(workspace / "repos" / "order-service", "remote", "set-url", "origin", str(renamed))

    raw = yaml.safe_load((workspace / "genome/knowledge/project-map.yaml").read_text())

    by_id = {module["id"]: module for module in raw["modules"]}
    assert by_id["order-service"]["path"] == "repos/order-service/"
    assert "repos/order-service" in (workspace / ".gitmodules").read_text()


def test_two_repos_with_the_same_name_both_land_intact(tmp_path: Path) -> None:
    """两个 org 下的同名仓一起挂。

    断言的重点不是"目录名不一样",是**两个仓的内容各自都在**——碰撞真正的危害是后挂的
    那个悄悄盖掉先挂的,而那种失败不会报错,只会让一个模块的代码莫名其妙变成另一个的。
    """
    from tests.fixtures.mall import materialize_repo

    first = materialize_repo("order-service", tmp_path / "team-a")
    second = materialize_repo("inventory-service", tmp_path / "team-b")
    # 两个来源不同、内容不同,但**挂载时同名**。
    renamed = second.remote.parent / "order-service.git"
    second.remote.rename(renamed)

    workspace = tmp_path / "ws"
    result = runner.invoke(
        app,
        [
            "init",
            "--local-only",
            str(workspace),
            "--repo",
            first.remote_url,
            "--repo",
            str(renamed),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (workspace / "repos" / "order-service" / "src" / "order").is_dir()
    assert (workspace / "repos" / "order-service-2" / "src" / "inventory").is_dir()


def test_init_leaves_business_repos_untouched(tmp_path: Path, mall) -> None:
    """业务仓零改造是硬约束:除了 Workspace 侧的 .gitmodules,不写一个字节。

    查的是业务仓的**远端**(init 真正接触到的东西),不是本地夹具工作树——
    工作树是 `git submodule add` 无论如何都碰不到的,拿它当断言对象永远是绿的。
    """
    before = {
        name: (
            _git(repo.remote, "rev-parse", "HEAD"),
            _git(repo.remote, "for-each-ref", "--format=%(refname) %(objectname)"),
        )
        for name, repo in mall.items()
    }

    _init(tmp_path / "ws", mall)

    for name, repo in mall.items():
        head, refs = before[name]
        assert _git(repo.remote, "rev-parse", "HEAD") == head, f"{name} 的 HEAD 被动了"
        # 引用全集不变:没有新分支、没有新 tag、没有任何旁路命名空间被写入。
        assert _git(repo.remote, "for-each-ref", "--format=%(refname) %(objectname)") == refs, (
            f"{name} 的引用被动了"
        )


def test_init_generates_a_project_map_skeleton_from_gitmodules(tmp_path: Path, mall) -> None:
    """确定性地产出骨架:id/path 填好,认知类字段留空待 knowledge-init 补齐。"""
    workspace = tmp_path / "ws"

    _init(workspace, mall)

    raw = yaml.safe_load((workspace / "genome/knowledge/project-map.yaml").read_text())
    assert raw["version"] == 1
    assert raw["project"]["name"] == "example-mall"

    by_id = {module["id"]: module for module in raw["modules"]}
    assert set(by_id) == {"order-service", "inventory-service"}
    assert by_id["order-service"]["path"] == "repos/order-service/"
    assert by_id["inventory-service"]["path"] == "repos/inventory-service/"
    assert by_id["order-service"].get("test_cmd") in (None, "")


def test_init_discovers_versioned_verification_specs_from_repository_entrypoints(
    tmp_path: Path, mall
) -> None:
    workspace = tmp_path / "ws"

    _init(workspace, mall)

    spec = load_verification_spec(workspace, "order-service")
    assert spec is not None
    assert spec.gate("unit").command.argv == ("make", "test")
    assert spec.gate("build").command.argv == ("make", "build")
    assert spec.gate("unit").provenance.producer == "makefile@1"


def test_init_output_is_loadable_and_validates(tmp_path: Path, mall) -> None:
    """init 之后 Workspace 必须立刻是一个合法状态,不必等 Agent 参与。"""
    workspace = tmp_path / "ws"

    _init(workspace, mall)

    result = runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "通过" in result.output


def test_init_commits_the_skeleton(tmp_path: Path, mall) -> None:
    workspace = tmp_path / "ws"

    _init(workspace, mall)

    assert _git(workspace, "status", "--porcelain") == ""
    assert "chore" in _git(workspace, "log", "-1", "--pretty=%s")


def test_tasks_directory_is_ignored(tmp_path: Path, mall) -> None:
    """运行态高频写入,入库会污染历史。"""
    workspace = tmp_path / "ws"
    _init(workspace, mall)

    (workspace / "tasks" / "ag-1").mkdir(parents=True)
    (workspace / "tasks" / "ag-1" / "task.json").write_text("{}")

    assert _git(workspace, "status", "--porcelain") == ""


def test_init_refuses_to_overwrite_an_existing_workspace(tmp_path: Path, mall) -> None:
    workspace = tmp_path / "ws"
    _init(workspace, mall)

    result = runner.invoke(
        app,
        ["init", "--local-only", str(workspace), "--repo", mall["order-service"].remote_url],
    )

    assert result.exit_code != 0
    assert "已存在" in result.output


def test_genome_validate_reports_readable_errors(tmp_path: Path, mall) -> None:
    workspace = tmp_path / "ws"
    _init(workspace, mall)
    write_flat_as_tree(
        workspace,
        {
            "version": 1,
            "project": {"name": "p"},
            "modules": [{"id": "order", "path": "repos/order-service/", "depends_on": ["ghost"]}],
        },
    )

    result = runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "ghost" in result.output
    assert "project-map.yaml" in result.output


def test_an_audit_bundle_does_not_dirty_the_workspace(tmp_path: Path, mall) -> None:
    """审计包是打包的快照,不是会演进的资产。入库会把协作仓撑爆。"""
    workspace = tmp_path / "ws"
    _init(workspace, mall)
    bundle = workspace / "archive" / "ag-20260901-001" / "ag-20260901-001-audit.zip"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"PK\x05\x06" + bytes(18))

    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert status.stdout == ""


def test_migrate_splits_a_flat_map_into_a_tree(tmp_path: Path, mall) -> None:
    """旧 Workspace 里躺着的是一份单文件地图。迁移只搬位置，一个字的知识都不改。"""
    import yaml

    workspace = tmp_path / "ws"
    _init(workspace, mall)
    flat = {
        "version": 3,
        "project": {"name": "example-mall", "summary": "电商中台"},
        "modules": [
            {
                "id": "order-service",
                "path": "repos/order-service/",
                "test_cmd": "pytest -q",
                "doc": "genome/knowledge/modules/order-service.md",
            }
        ],
        "interfaces": [
            {"id": "reserve-api", "kind": "http", "provider": "order-service"},
        ],
    }
    for stale in (workspace / "genome/knowledge/modules").glob("*"):
        if stale.is_dir():
            shutil.rmtree(stale)
    (workspace / "genome/knowledge/project-map.yaml").write_text(
        yaml.safe_dump(flat, allow_unicode=True), encoding="utf-8"
    )
    (workspace / "genome/knowledge/modules/order-service.md").write_text("# 订单域\n")

    result = runner.invoke(app, ["genome", "migrate", "--workspace", str(workspace), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["migrated"] is True
    module_map = yaml.safe_load(
        (workspace / "genome/knowledge/modules/order-service/map.yaml").read_text()
    )
    assert module_map["test_cmd"] == "pytest -q"
    assert module_map["features"] == [], "迁移不该凭空捏造功能点"
    assert (workspace / "genome/knowledge/modules/order-service/overview.md").is_file()
    assert not (workspace / "genome/knowledge/modules/order-service.md").exists()
    contracts = yaml.safe_load((workspace / "genome/knowledge/interfaces.yaml").read_text())
    assert contracts["interfaces"][0]["id"] == "reserve-api"

    # 迁移后的树能通过校验，且重跑无变化。
    assert runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)]).exit_code == 0
    again = runner.invoke(app, ["genome", "migrate", "--workspace", str(workspace), "--json"])
    assert json.loads(again.output)["migrated"] is False


def test_a_flat_map_tells_you_to_migrate(tmp_path: Path, mall) -> None:
    """严格模式会把旧文件报成"多了个 interfaces 字段"，读起来像拼写错误。"""
    import yaml

    workspace = tmp_path / "ws"
    _init(workspace, mall)
    (workspace / "genome/knowledge/project-map.yaml").write_text(
        yaml.safe_dump(
            {"version": 1, "project": {"name": "p"}, "modules": [], "interfaces": []},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "migrate" in result.output


def _add_feature(workspace: Path, **overrides: object) -> None:
    import yaml

    target = workspace / "genome/knowledge/modules/order-service/map.yaml"
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["features"] = [
        {
            "id": "reserve-flow",
            "summary": "下单预占",
            "scope": ["repos/order-service/**"],
            **overrides,
        }
    ]
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def test_validate_refuses_a_feature_with_no_knowledge_and_no_declaration(
    tmp_path: Path, mall
) -> None:
    """地图上不允许存在缺口。这是 ADR-0003 在命令行上的样子。"""
    workspace = tmp_path / "ws"
    _init(workspace, mall)
    _add_feature(workspace)

    result = runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "reserve-flow" in result.output
    assert "缺口" in result.output


def test_validate_lists_every_no_card_declaration_for_review(tmp_path: Path, mall) -> None:
    """「不需要知识」是一个被记录下来的判断，所以它要被看见。"""
    workspace = tmp_path / "ws"
    _init(workspace, mall)
    _add_feature(workspace, no_card="纯 CRUD，scope 内无隐含约定")

    result = runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "1 个功能点" in result.output
    assert "纯 CRUD" in result.output


def test_validate_refuses_an_oversized_card_and_says_by_how_much(tmp_path: Path, mall) -> None:
    """只说「不让过」的话，收到拒绝的人不知道该拆哪里，于是会去改预算而不是去分形。"""
    import yaml

    workspace = tmp_path / "ws"
    _init(workspace, mall)
    _add_feature(workspace, card="features/reserve-flow.md")
    card = workspace / "genome/knowledge/modules/order-service/features/reserve-flow.md"
    card.parent.mkdir(parents=True)
    card.write_text(
        "---\nid: reserve-flow\nsummary: 下单预占\n---\n" + "填充。\n" * 400, encoding="utf-8"
    )
    config = workspace / "agentgenome.yaml"
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    payload["knowledge"] = {"card_lines": 10}
    config.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    result = runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "行数预算" in result.output
    assert "10 行" in result.output


def test_validate_warns_about_fragmentation_without_failing(tmp_path: Path, mall) -> None:
    """切碎有时是对的，但它应该是被注意到的。"""
    import yaml

    workspace = tmp_path / "ws"
    _init(workspace, mall)
    target = workspace / "genome/knowledge/modules/order-service/map.yaml"
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    payload["features"] = [
        {
            "id": f"f{i}",
            "summary": f"f{i}",
            "scope": ["repos/order-service/**"],
            "card": f"features/f{i}.md",
        }
        for i in range(4)
    ]
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    for i in range(4):
        card = workspace / f"genome/knowledge/modules/order-service/features/f{i}.md"
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text(f"---\nid: f{i}\nsummary: f{i}\n---\n细节。\n", encoding="utf-8")

    result = runner.invoke(app, ["genome", "validate", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "被 4 个带卡片的功能点同时覆盖" in result.output


# --- 挂载失败:要么全成,要么不留痕 ---------------------------------------------
#
# 骨架先写、业务仓后挂。任何一个仓挂不上,磁盘上都已经留下半个 Workspace,而重跑会被
# "已存在"挡住——**命令失败了,而最自然的补救被拒绝**。用户唯一的出路是自己 rm -rf,
# 且没有任何东西告诉他这件事。


def _empty_remote(tmp_path: Path) -> str:
    """一个连提交都没有的裸仓。绿地开发最自然的起点:刚在托管平台点了新建。"""
    remote = tmp_path / "brand-new.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "--initial-branch=main", str(remote)], check=True
    )
    return str(remote)


def test_mounting_an_empty_repo_leaves_nothing_behind(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"

    result = runner.invoke(
        app, ["init", "--local-only", str(workspace), "--repo", _empty_remote(tmp_path)]
    )

    assert result.exit_code != 0
    assert not workspace.exists(), "失败的初始化把目标目录占住了"


def test_an_empty_repo_is_told_to_make_a_first_commit(tmp_path: Path) -> None:
    """git 原文是"branch yet to be born",既不说是哪个仓也不说该做什么。"""
    result = runner.invoke(
        app, ["init", "--local-only", str(tmp_path / "ws"), "--repo", _empty_remote(tmp_path)]
    )

    assert "brand-new" in result.output
    assert "初始提交" in result.output


def test_an_unreachable_address_says_which_one(tmp_path: Path) -> None:
    """挂五个仓时不该让人逐个试。"""
    workspace = tmp_path / "ws"

    result = runner.invoke(
        app, ["init", "--local-only", str(workspace), "--repo", str(tmp_path / "nope.git")]
    )

    assert result.exit_code != 0
    assert "nope.git" in result.output
    assert not workspace.exists()


def test_a_missing_branch_names_the_repo_and_the_branch(tmp_path: Path, mall) -> None:
    """让人知道该去查的是分支名,而不是地址。"""
    workspace = tmp_path / "ws"

    result = runner.invoke(
        app,
        [
            "init",
            "--local-only",
            str(workspace),
            "--repo",
            f"{mall['order-service'].remote_url}@nosuchbranch",
        ],
    )

    assert result.exit_code != 0
    assert "nosuchbranch" in result.output
    assert "order-service" in result.output
    assert not workspace.exists()


def test_a_later_repo_failing_rolls_back_the_earlier_ones(tmp_path: Path, mall) -> None:
    """不是"留下第一个已挂好的"——半个 Workspace 与整个一样挡路。"""
    workspace = tmp_path / "ws"

    result = runner.invoke(
        app,
        [
            "init",
            "--local-only",
            str(workspace),
            "--repo",
            mall["order-service"].remote_url,
            "--repo",
            str(tmp_path / "nope.git"),
        ],
    )

    assert result.exit_code != 0
    assert not workspace.exists()


def test_retrying_after_fixing_the_address_just_works(tmp_path: Path, mall) -> None:
    """**这是这一片的核心承诺。** 失败之后把地址改对,原样重跑能成。"""
    workspace = tmp_path / "ws"
    runner.invoke(
        app, ["init", "--local-only", str(workspace), "--repo", str(tmp_path / "nope.git")]
    )

    result = runner.invoke(
        app,
        [
            "init",
            "--local-only",
            str(workspace),
            "--name",
            "example-mall",
            "--repo",
            mall["order-service"].remote_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert (workspace / "repos" / "order-service" / "src").is_dir()


def test_a_pre_created_empty_directory_still_gets_cleaned_up(tmp_path: Path) -> None:
    """`mkdir ws && agctl init ws` 是再正常不过的用法。

    只看"目录存在不存在"来决定清不清的话,这条会走进最坏的组合:目录本来就在,于是失败之后
    不清,而留下的半个骨架又让重跑撞上"已存在"——人明明什么都没放进去,却被当成了别人的东西。
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()

    result = runner.invoke(
        app, ["init", "--local-only", str(workspace), "--repo", str(tmp_path / "nope.git")]
    )

    assert result.exit_code != 0
    assert not any(workspace.iterdir()), "预先建好的空目录里留下了半个 Workspace"


def test_an_untranslated_git_error_keeps_its_original_text(tmp_path: Path, mall) -> None:
    """翻译不全时保留原文,比吞掉强。

    认不出的失败**必须**把 git 说的话原样带出来——否则人手上既没有译文也没有原文,
    唯一的线索只剩"挂不上"三个字。
    """
    hostile = tmp_path / "hostile.git"
    subprocess.run(["git", "init", "--bare", "-q", str(hostile)], check=True)
    # 一个空仓 + 一个指定分支:git 报的是"分支还没出生",而不是我们认得的那几句地址错误。
    result = runner.invoke(
        app, ["init", "--local-only", str(tmp_path / "ws"), "--repo", f"{hostile}@nosuchbranch"]
    )

    assert result.exit_code != 0
    assert "hostile" in result.output


def test_a_pre_existing_directory_is_never_touched(tmp_path: Path) -> None:
    """只清自己建的。一个失败的初始化不该有权删掉别人的东西。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    keeper = workspace / "important.txt"
    keeper.write_text("别人的东西\n", encoding="utf-8")

    result = runner.invoke(
        app, ["init", "--local-only", str(workspace), "--repo", str(tmp_path / "nope.git")]
    )

    assert result.exit_code != 0
    assert keeper.read_text(encoding="utf-8") == "别人的东西\n"


def test_an_existing_workspace_is_still_refused_and_left_alone(tmp_path: Path, mall) -> None:
    """ "已存在"那道保护原样保留——一次误敲不该抹掉已经积累的基因组。"""
    workspace = tmp_path / "ws"
    _init(workspace, mall)
    before = (workspace / "genome" / "rules" / "architecture.md").read_text(encoding="utf-8")

    result = runner.invoke(
        app, ["init", "--local-only", str(workspace), "--repo", mall["order-service"].remote_url]
    )

    assert result.exit_code != 0
    assert "已存在" in result.output
    assert (workspace / "genome" / "rules" / "architecture.md").read_text() == before
