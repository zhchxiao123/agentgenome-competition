"""`--repo` 取值的解析:URL 与可选的每仓分支。"""

from __future__ import annotations

import pytest

from agentgenome.genome.scaffold import (
    derive_module_id,
    parse_repo_arg,
    plan_repos,
    slugify_mount,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "https://git.example.com/org/order-service.git",
            ("https://git.example.com/org/order-service.git", None),
        ),
        (
            "https://git.example.com/org/order-service.git@develop",
            ("https://git.example.com/org/order-service.git", "develop"),
        ),
        ("/tmp/upstream/order-service.git", ("/tmp/upstream/order-service.git", None)),
        ("/tmp/upstream/order-service.git@main", ("/tmp/upstream/order-service.git", "main")),
    ],
)
def test_parses_url_and_optional_branch(value, expected):
    assert parse_repo_arg(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "git@github.com:org/repo.git",  # SSH:@ 后面含 : 与 /
        "ssh://git@host/org/repo.git",  # SSH URL
    ],
)
def test_ssh_addresses_are_not_mistaken_for_a_branch(value):
    """`git@host:org/repo.git` 里的 @ 不是分支分隔符,切错会让挂载直接失败。"""
    assert parse_repo_arg(value) == (value, None)


def test_branch_travels_into_the_mount_plan():
    specs = plan_repos(
        [
            "https://git.example.com/org/order-service.git@develop",
            "https://git.example.com/org/inventory-service.git",
        ]
    )

    assert [(s.module_id, s.path, s.branch) for s in specs] == [
        ("order-service", "repos/order-service/", "develop"),
        ("inventory-service", "repos/inventory-service/", None),
    ]


@pytest.mark.parametrize(
    ("url", "module_id"),
    [
        ("https://git.example.com/org/order-service.git", "order-service"),
        ("https://git.example.com/org/order-service", "order-service"),
        ("/tmp/up/inventory-service.git", "inventory-service"),
        ("git@github.com:org/repo.git", "repo"),
    ],
)
def test_module_id_comes_from_the_repo_name(url, module_id):
    """路径是位置,id 是身份——id 取仓库名,与挂载点的净化规则互不干涉。"""
    assert derive_module_id(url) == module_id


# --- slug 净化 ---------------------------------------------------------------
#
# `code-N` 那套编号免费带着两条性质:任何仓库名都产不出非法路径,同名仓库也永不碰撞。
# 换成语义化路径之后,这两条得自己挣回来。


@pytest.mark.parametrize(
    ("module_id", "slug"),
    [
        # 已经合法的原样通过——绝大多数仓库走的是这一条。
        ("order-service", "order-service"),
        ("api_v2", "api_v2"),
        ("web.client", "web.client"),
        # 大小写:目录名在 macOS 与 Windows 上不区分大小写,留着大写等于留一颗
        # "在我机器上是两个目录、在 CI 上是一个"的雷。
        ("Order-Service", "order-service"),
        # 非法或危险字符折成单个 `-`,不是每字符一个 `-`。
        ("my repo", "my-repo"),
        ("a//b", "a-b"),
        ("order:service", "order-service"),
        # 非 ASCII 全部折掉。**刻意不保留**:macOS 用 NFD、Linux 用 NFC,同一个名字
        # 在两处是不同的字节序列,而挂载点要进 `.gitmodules` 并被逐字比对。
        ("后端服务", "module"),
        # 首尾的 `-` 与 `.` 去掉:`.foo` 是隐藏目录,`foo.` 在 Windows 上创建不了。
        ("-lead", "lead"),
        ("trail-", "trail"),
        (".hidden", "hidden"),
        ("dotted.", "dotted"),
        # 净化后什么都不剩时退化,而不是产出一个空目录名。
        ("---", "module"),
        ("...", "module"),
    ],
)
def test_slugify_makes_a_safe_directory_name(module_id, slug):
    assert slugify_mount(module_id) == slug


@pytest.mark.parametrize("reserved", ["con", "CON", "Nul", "com1", "LPT9", "aux", "prn"])
def test_windows_reserved_names_are_dodged(reserved):
    """这些名字在 Windows 上**任何扩展名下都创建不了**。

    `code-N` 时代不可能撞上,因为挂载点根本不来自仓库名;现在它是仓库名的直接函数,
    而 `nul` 这种仓库名是真实存在的。撞上的表现是 clone 在 Windows 上直接失败——
    一个只在部分开发者机器上出现、且完全不指向根因的故障。
    """
    assert slugify_mount(reserved) != reserved.lower()
    assert slugify_mount(reserved).startswith(reserved.lower())


def test_a_dot_is_never_a_mount_point():
    """`.` 与 `..` 会让挂载点指向 Workspace 根或它的父目录。"""
    assert slugify_mount(".") == "module"
    assert slugify_mount("..") == "module"


# --- 同名冲突 ---------------------------------------------------------------


def test_two_repos_with_the_same_name_get_distinct_mount_points():
    specs = plan_repos(
        [
            "https://git.example.com/team-a/api.git",
            "https://git.example.com/team-b/api.git",
        ]
    )

    assert [s.path for s in specs] == ["repos/api/", "repos/api-2/"]
    # 身份不跟着位置走:两个仓的 module id 仍然都是仓库名本身。
    assert [s.module_id for s in specs] == ["api", "api"]


def test_the_collision_suffix_counts_up():
    specs = plan_repos([f"https://git.example.com/org-{i}/api.git" for i in range(3)])

    assert [s.path for s in specs] == ["repos/api/", "repos/api-2/", "repos/api-3/"]


def test_collision_is_judged_on_the_sanitised_slug():
    """`Order-Service` 与 `order-service` 是两个不同的仓库名,净化之后是同一个目录。

    只比**原始名**的话这两个会被判成不冲突,然后挂到同一个路径上——第二次
    `git submodule add` 失败,或者更糟,悄悄覆盖第一个。

    断言拿的是精确路径而不是"两条不一样":后者在两个仓从头到尾就没碰撞过时也成立,
    是一条永远绿的断言。
    """
    specs = plan_repos(
        [
            "https://git.example.com/a/Order-Service.git",
            "https://git.example.com/b/order-service.git",
        ]
    )

    assert [s.path for s in specs] == ["repos/order-service/", "repos/order-service-2/"]


def test_underscore_and_hyphen_are_not_treated_as_a_collision():
    """`order_service` 与 `order-service` 是两个能共存的目录,**不是**冲突。

    把 `_` 折成 `-` 会让它们撞上,而那样做一分安全都不买(没有任何文件系统把这两个名字
    当同一个),只损失对仓库名的忠实度。所以 `_` 留在安全字符集里。
    """
    specs = plan_repos(
        [
            "https://git.example.com/a/order-service.git",
            "https://git.example.com/b/order_service.git",
        ]
    )

    assert [s.path for s in specs] == ["repos/order-service/", "repos/order_service/"]
