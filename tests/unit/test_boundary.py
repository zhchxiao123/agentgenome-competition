"""② 模块边界草案:人在整条流水线里唯一要做判断的那一刻,看到的是什么。

**这份草案里的每句话都是给人当尺子用的。** 说错一句的代价不是难看,是人拿着错尺子去复核——
而他复核完就往下跑了,后面所有的知识都长在那个判断上。

这个文件此前不存在,而这正是四条假话能一起静默上线的原因:唯一的间接覆盖只断言"划分依据非空",
而一句假话也是非空的。
"""

from __future__ import annotations

from agentgenome.genome.boundary import propose_boundaries
from agentgenome.genome.scan import Candidate, HotPath, MountState, ScanResult


def _scan(*candidates: Candidate, hot: tuple[HotPath, ...] = ()) -> ScanResult:
    return ScanResult(candidates=candidates, hot_paths=hot, since_days=180)


def _candidate(
    path: str,
    *,
    language: str | None = "python",
    build_files: tuple[str, ...] = ("pyproject.toml",),
    dependencies: tuple[str, ...] = ("httpx",),
    state: MountState = MountState.POPULATED,
) -> Candidate:
    return Candidate(
        path=path,
        language=language,
        build_files=build_files,
        dependencies=dependencies,
        state=state,
    )


def _bare(path: str, state: MountState) -> Candidate:
    """一个什么内容都没有的候选。三态里除"有内容"之外的两种天然长这样。"""
    return _candidate(path, language=None, build_files=(), dependencies=(), state=state)


def _only(scan: ScanResult) -> dict:
    return propose_boundaries(scan)["modules"][0]


# --- 划分依据据实构造 ---------------------------------------------------------


def test_a_candidate_with_build_files_says_which_ones() -> None:
    rationale = _only(_scan(_candidate("repos/order-service")))["rationale"]

    assert "pyproject.toml" in rationale


def test_a_candidate_without_build_files_does_not_claim_to_have_them() -> None:
    """此前这句是**无条件**拼上去的,于是没有构建文件的仓渲染成 `有独立的构建文件()`。

    一句自相矛盾的话比没有这句更糟:人会以为自己看漏了,而不是以为系统写错了。
    """
    rationale = _only(
        _scan(_candidate("repos/docs", language=None, build_files=(), dependencies=()))
    )["rationale"]

    assert "构建文件" not in rationale
    assert "()" not in rationale
    assert rationale, "划分依据不能为空——人靠它复核"


def test_the_language_is_only_mentioned_when_it_is_known() -> None:
    rationale = _only(_scan(_candidate("repos/docs", language=None, build_files=())))["rationale"]

    assert "语言" not in rationale


# --- 热区按目录边界比 ---------------------------------------------------------


def test_a_candidate_with_recent_churn_is_flagged() -> None:
    """常改的地方值得人多看一眼:它要么是核心,要么是没被理顺的那块。"""
    scan = _scan(
        _candidate("repos/order-service"),
        hot=(HotPath(path="repos/order-service/src/app.py", changes=9),),
    )

    assert "近期变更频繁" in _only(scan)["rationale"]


def test_churn_in_a_sibling_repo_does_not_flag_this_one() -> None:
    """`repos/api` 与 `repos/api-2` 是两个仓。

    这一对不是杜撰的——同名仓库挂第二次拿到的就是 `-2` 后缀,所以"前缀相同的兄弟目录"
    在这套挂载约定下是常规产物。
    """
    scan = _scan(
        _candidate("repos/api"),
        hot=(HotPath(path="repos/api-2/src/app.py", changes=9),),
    )

    assert "近期变更频繁" not in _only(scan)["rationale"]


def test_churn_is_matched_below_the_mount_root_not_at_it() -> None:
    """**这条钉的是一个真出过的 bug。**

    判定曾经取变更路径的第一段。挂载点是一级目录时,第一段正好等于候选路径,于是它一直是对的;
    挂载点变成 `repos/<仓>/` 之后,第一段恒为挂载根,永远不等于任何候选——这条提示就此
    永久失效,而没有任何测试发现。
    """
    scan = _scan(
        _candidate("repos/order-service"),
        _candidate("repos/inventory-service"),
        hot=(HotPath(path="repos/order-service/src/app.py", changes=9),),
    )
    modules = propose_boundaries(scan)["modules"]

    flagged = [item["id"] for item in modules if "近期变更频繁" in item["rationale"]]
    assert flagged == ["order-service"]


# --- 身份 --------------------------------------------------------------------


def test_the_proposed_id_is_the_last_segment_of_the_mount_point() -> None:
    """模块 id 是门禁配置的**文件名索引**,带路径分隔符会落到一层意料之外的嵌套目录。

    取末段也让草案的默认提议与初始化写进根索引的 id 一致——同一个仓在两处给出不同的 id
    没有任何理由。
    """
    module = _only(_scan(_candidate("repos/order-service")))

    assert module["id"] == "order-service"
    assert "/" not in module["id"]


def test_the_path_keeps_the_full_mount_point() -> None:
    """id 是身份、path 是位置,两者刻意不耦合——收窄 id 不能顺手把位置也削掉。"""
    module = _only(_scan(_candidate("repos/order-service")))

    assert module["path"] == "repos/order-service/"


# --- 给人的说明 --------------------------------------------------------------


def test_the_note_states_the_rule_that_is_actually_in_force() -> None:
    """判据早已改成"挂载了就算一个候选",而这句话还在讲构建文件。

    人会按它去复核草案。说错等于给了他一把错的尺子。
    """
    note = propose_boundaries(_scan(_candidate("repos/order-service")))["note"]

    assert "有构建文件就算一个模块" not in note
    assert "挂载" in note


def test_the_note_still_asks_the_human_to_merge_and_split() -> None:
    """闸门存在的全部理由。丢了这句,人会以为自己只是在点确认。"""
    note = propose_boundaries(_scan(_candidate("repos/order-service")))["note"]

    assert "合并" in note
    assert "拆" in note


# --- 三态各说各的话 -----------------------------------------------------------


def test_an_empty_repo_says_it_has_no_code_yet() -> None:
    """绿地新仓。**这是正常状态**,措辞不能像在报错。"""
    rationale = _only(_scan(_bare("repos/greenfield", MountState.EMPTY)))["rationale"]

    assert "还没有代码" in rationale
    assert "构建文件" not in rationale


def test_an_unready_mount_point_says_it_is_not_checked_out() -> None:
    """环境没就绪。人要做的是修环境,不是在这里做判断——措辞必须把这件事分开。"""
    rationale = _only(_scan(_bare("repos/not-pulled", MountState.UNREADY)))["rationale"]

    assert "checkout" in rationale.lower() or "拉下来" in rationale


def test_an_empty_repo_and_a_docs_repo_do_not_read_the_same() -> None:
    """两者都没有构建文件,但一个是"还没开始写"、一个是"就是这样"。

    读起来一样的话,人就没法判断该等它长出来还是该现在拍板。
    """
    empty = _only(
        _scan(
            _candidate(
                "repos/a", language=None, build_files=(), dependencies=(), state=MountState.EMPTY
            )
        )
    )["rationale"]
    docs = _only(
        _scan(
            _candidate(
                "repos/b",
                language=None,
                build_files=(),
                dependencies=(),
                state=MountState.POPULATED,
            )
        )
    )["rationale"]

    assert empty != docs


# --- 两个入口共用的前置检查 -----------------------------------------------------


def test_planning_stops_when_a_mount_point_is_not_checked_out(tmp_path) -> None:
    """**这条钉的是"两条路各写一遍"那个形态。**

    划边界的入口有两条(命令行直接规划、基因组任务从扫描态被推进)。此前拦截只加在其中一条,
    而没加的那条恰恰是恢复路径——`plan` 在建任务之后、草案就绪之前挂掉时走的就是它。
    """
    import pytest

    from agentgenome.genome.boundary import NotReadyForBoundaries, scan_for_boundaries

    (tmp_path / "repos" / "pulled").mkdir(parents=True)
    (tmp_path / "repos" / "pulled" / ".git").write_text("gitdir: x\n", encoding="utf-8")
    (tmp_path / "repos" / "not-pulled").mkdir(parents=True)
    (tmp_path / ".gitmodules").write_text(
        '[submodule "repos/pulled"]\n\tpath = repos/pulled\n\turl = ../a.git\n'
        '[submodule "repos/not-pulled"]\n\tpath = repos/not-pulled\n\turl = ../b.git\n',
        encoding="utf-8",
    )

    with pytest.raises(NotReadyForBoundaries) as error:
        scan_for_boundaries(tmp_path, since_days=180)

    assert "repos/not-pulled" in str(error.value)
    assert "git submodule update --init" in str(error.value)


def test_planning_stops_when_nothing_is_mounted(tmp_path) -> None:
    import pytest

    from agentgenome.genome.boundary import NotReadyForBoundaries, scan_for_boundaries

    with pytest.raises(NotReadyForBoundaries) as error:
        scan_for_boundaries(tmp_path, since_days=180)

    # 说得出该去看哪儿,而不是"先 init 挂上再来"——那条在一个已经存在的 Workspace 上做不到。
    assert ".gitmodules" in str(error.value)
