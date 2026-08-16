"""knowledge-deepen:单功能点三源深化(PRD 41)。

init 出骨架,深化出血肉。一趟 init 摊到十九个功能点,每张卡只够扫一眼——薄不是员工
偷懒,是上下文预算摊薄的必然结果。所以深化是**独立工序**:一个作业只深读一个功能点,
这是 deep_read「不能一个作业读完全库」原则再下沉一层。

## 三源法

作业的输入只有三个来源:该功能点 scope 覆盖的代码本体、它的测试、最近 10 个触碰
commit。不变量藏在 fix/revert 历史与测试里奇怪而具体的断言中——先挖再写,写不出
出处的认知宁可标低置信度。

## 通道只装一张卡

产出走 PRD 34 的 staging 通道,但 staging 里只允许出现
`modules/<模块id>/features/<功能点id>.md` 这一个文件——多写的按越界拒绝(同 focus
语义)。卡片本身的纪律与整树 staging 完全相同(`staging.check_card_file` 是唯一出口):
锚点必真、行数预算、front matter 规则,一条不松。

**深化不改清单。** 功能点条目(scope/summary)是地图的事,深化只增厚卡片正文;
所以应用也只写这一个文件,不碰 map.yaml——通道越窄,越界越无处藏身。

## 排队按热区

深化队列按 scope 覆盖文件的变更热度(churn)降序——中途停下时,已深化的恰好是
最常被任务碰到的那批卡。队列本身是纯函数(输入是树与计数),git 的部分单独一层薄壳,
与 `routing.route` 同一个测试哲学。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentgenome.config import KnowledgeConfig
from agentgenome.core.scope import compile_glob
from agentgenome.genome.errors import GenomeValidationError, ValidationIssue
from agentgenome.genome.features import FeatureEntry, KnowledgeTree
from agentgenome.genome.knowledge import (
    CARD_DISCIPLINE,
    CARD_SKELETON,
    RECEIPT_SCHEMA,
    output_dir_display,
)
from agentgenome.genome.staging import (
    HUMAN_EDITED_MARKER,
    STAGING_DIR,
    Snapshot,
    check_card_file,
    render_issues,
)
from agentgenome.genome.tree import module_dir

PROCEDURE_ID = "knowledge-deepen"
PROCEDURE_VERSION = "1.0.0"
EMPLOYEE_ID = "arch-employee"

#: 每个模块看多深的提交历史。够覆盖"最近谁最热",又不至于把十年前的重构算成热区。
CHURN_DEPTH = 200


@dataclass(frozen=True)
class DeepenCandidate:
    """深化队列里的一项:哪张卡、有多热。"""

    module_id: str
    feature_id: str
    summary: str
    scope: tuple[str, ...]
    churn: int


def deepen_queue(tree: KnowledgeTree, counts: Mapping[str, int]) -> tuple[DeepenCandidate, ...]:
    """待深化的功能点,churn 降序。**纯函数**:输入是树与计数,不做 I/O。

    - `no_card` 不入队:声明过"这里不需要知识"的地方没有卡可深。
    - 人工定稿(卡片带 `HUMAN_EDITED_MARKER`)不入队:应用会拒绝覆盖它,
      入队只会白烧一个作业。
    """
    candidates: list[DeepenCandidate] = []
    for module_id, features in tree.feature_index.items():
        for feature in features:
            if feature.card is None:
                continue
            card = tree.card(module_id, feature.id)
            if card is not None and HUMAN_EDITED_MARKER in card.body:
                continue
            matchers = [compile_glob(scope) for scope in feature.scope]
            churn = sum(
                count
                for path, count in counts.items()
                if any(matcher.match(path) for matcher in matchers)
            )
            candidates.append(
                DeepenCandidate(
                    module_id=module_id,
                    feature_id=feature.id,
                    summary=feature.summary,
                    scope=tuple(feature.scope),
                    churn=churn,
                )
            )
    return tuple(
        sorted(candidates, key=lambda item: (-item.churn, item.module_id, item.feature_id))
    )


def churn_counts(
    workspace_root: Path, module_paths: Mapping[str, str], depth: int = CHURN_DEPTH
) -> dict[str, int]:
    """每个文件最近被提交碰过几次,键是 Workspace 相对路径。

    git 只是数据源,判断都在 `deepen_queue` 里——所以这里读不出历史(不是 git 仓、
    命令失败)就安静地少算,队列退化成"全部零热度、按 id 稳定排序",而不是整条命令炸掉。
    """
    counts: dict[str, int] = {}
    for module_path in module_paths.values():
        base = Path(workspace_root) / module_path
        if not (base / ".git").exists():
            continue
        proc = subprocess.run(  # noqa: S603 - 参数全部程序内拼装,无用户输入
            ["git", "-C", str(base), "log", "--format=", "--name-only", "-n", str(depth)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            continue
        prefix = module_path.rstrip("/")
        for line in proc.stdout.splitlines():
            name = line.strip()
            if name:
                key = f"{prefix}/{name}"
                counts[key] = counts.get(key, 0) + 1
    return counts


# --- staging 校验与应用 --------------------------------------------------------


def _expected_card(module_id: str, feature_id: str) -> PurePosixPath:
    return PurePosixPath("modules") / module_id / "features" / f"{feature_id}.md"


def validate_deepen_staging(
    workspace_root: Path,
    staging_root: Path,
    module_id: str,
    feature_id: str,
    module_path: str,
    limits: KnowledgeConfig | None = None,
) -> list[ValidationIssue]:
    """深化产物的确定性校验。**staging 里只许那一张卡。**"""
    expected = _expected_card(module_id, feature_id)
    staging_root = Path(staging_root)
    files = (
        sorted(item for item in staging_root.rglob("*") if item.is_file())
        if staging_root.is_dir()
        else []
    )
    if not files:
        return [
            ValidationIssue(
                f"{STAGING_DIR}/",
                "没有产物:staging/ 不存在或为空。深化的认知必须写成"
                f" {STAGING_DIR}/{expected} 这一份真实文件,不是对话里的总结",
            )
        ]
    issues: list[ValidationIssue] = []
    card_file: Path | None = None
    for item in files:
        relative = PurePosixPath(item.relative_to(staging_root))
        if relative == expected:
            card_file = item
            continue
        issues.append(
            ValidationIssue(
                str(PurePosixPath(STAGING_DIR) / relative),
                f"越界:深化 {module_id}/{feature_id} 只允许写 {STAGING_DIR}/{expected}"
                " 这一张卡,其余功能点由各自的深化作业负责",
            )
        )
    if card_file is None:
        issues.append(
            ValidationIssue(
                f"{STAGING_DIR}/{expected}",
                f"这一趟要深化 {module_id}/{feature_id},staging 里却没有它的卡片",
            )
        )
        return issues
    _, card_issues = check_card_file(
        workspace_root, staging_root, feature_id, card_file, module_path, limits
    )
    return [*issues, *card_issues]


def deepen_output_check(
    workspace_root: Path,
    module_id: str,
    feature_id: str,
    module_path: str,
    limits: KnowledgeConfig | None = None,
) -> Callable[[Path], str | None]:
    """给 `JobSpec.output_check` 用的裁决闭包。失败按格式失败走契约重试(增量修复)。"""
    root = Path(workspace_root)

    def check(output_dir: Path) -> str | None:
        issues = validate_deepen_staging(
            root, output_dir / STAGING_DIR, module_id, feature_id, module_path, limits
        )
        return render_issues(issues) if issues else None

    return check


@dataclass(frozen=True)
class DeepenOutcome:
    """一次深化应用的结果。"""

    #: 真的写了吗。`False` = 卡片带人工编辑标记,原文保留。
    written: bool
    #: 卡片文件的 Workspace 相对路径。
    path: str


def apply_deepened(
    workspace_root: Path,
    module_id: str,
    feature_id: str,
    card_text: str,
    limits: KnowledgeConfig | None = None,
) -> DeepenOutcome:
    """把一张已验证的深化卡写进树。**只写这一个文件,不碰清单。**

    人工定过稿的卡片(`HUMAN_EDITED_MARKER`)不覆盖。写完整树校验一遍——卡片正文
    改不动树的形状,但行数预算等约束仍在;不过的话逐字节还原,半更新状态的树不合法。
    """
    from agentgenome.genome.budgets import check_budgets
    from agentgenome.genome.loader import load_tree

    root = Path(workspace_root)
    target = module_dir(root, module_id) / "features" / f"{feature_id}.md"
    relative = str(target.relative_to(root))
    if target.is_file() and HUMAN_EDITED_MARKER in target.read_text(encoding="utf-8"):
        return DeepenOutcome(written=False, path=relative)
    restore = Snapshot(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(card_text, encoding="utf-8")
    try:
        tree = load_tree(root)
        over = check_budgets(root, tree, limits or KnowledgeConfig())
        if over:
            raise GenomeValidationError(over)
    except GenomeValidationError:
        restore.rollback()
        raise
    return DeepenOutcome(written=True, path=relative)


# --- 提示词 --------------------------------------------------------------------


def build_prompt(
    module_id: str,
    module_path: str,
    feature: FeatureEntry,
    current_card: str,
    workspace_root: Path,
    output_dir: Path | None = None,
    test_cmd: str | None = None,
) -> str:
    """深化作业的上下文包。**有界**:只有这个功能点的 scope、测试与提交历史指引。

    不注入项目地图——深化不判归属、不报契约,给全库内容只会把三源的预算挤掉,
    而那正是 init 把卡写薄的原因。
    """
    staging = output_dir_display(output_dir, workspace_root) + "/" + STAGING_DIR
    card_path = f"{staging}/{_expected_card(module_id, feature.id)}"
    scopes = "\n".join(f"- `{scope}`" for scope in feature.scope)
    base = module_path.rstrip("/")
    current = current_card.strip() or "(还没有卡片正文)"
    tests = f"模块测试命令:`{test_cmd}`。" if test_cmd else ""
    return f"""# knowledge-deepen · {module_id}/{feature.id}

你是 AgentGenome 的架构数字员工。这一趟只深化**一个功能点**的认知卡片:读透它的
代码、测试与提交历史,把现有薄卡增厚成一页判断。

## 铁律

1. **只读代码,不改任何业务文件。** 你的产出是一张卡片文件与一张小票。
2. **staging 里只允许这一个文件:`{card_path}`。** 多写的文件会被校验按越界拒绝——
   其余功能点由各自的深化作业负责。
3. **不确定的认知标注 `confidence` 而非编造。** 写不出出处的结论宁可不写。

## 这个功能点

- id:`{feature.id}`
- summary:{feature.summary}
- scope(Workspace 相对):
{scopes}
- 模块目录:`{base}/`。{tests}

## 现有卡片(增厚它,别从头编)

```markdown
{current}
```

## 三个来源,都要挖

1. **代码本体**:通读 scope 覆盖的文件。
2. **它的测试**:找到钉住这段代码的测试;**奇怪而具体的断言是化石化的不变量**
   ("为什么这里要断言 X ≠ Y"——答案就是一条承重不变量)。
3. **提交历史**:`git -C {base} log -p -10 -- <路径>`(路径用模块目录相对)。
   fix/revert 蕴含的 never-again 规则就是坑点,提交号就是它的出处。

## 产出:把卡写进 staging/

`{card_path}`(front matter 的 `id` 必须是 `{feature.id}`):

{CARD_SKELETON}

{CARD_DISCIPLINE}

## 小票:result.json

卡写完之后,在产物目录写一张扁平小票(不放任何文件内容进来):

```json
{{
  "task_id": "knowledge-deepen",
  "producer": "arch-employee",
  "notes": ["值得一提的观察"],
  "questions": ["拿不准、需要人拍板的问题"],
  "low_confidence": []
}}
```

## 如果上一次的产出被拒绝了

拒绝原因是逐文件的问题清单,staging 里已有的文件都还在:只修被点名的问题。

Workspace 根目录:{workspace_root}
"""


__all__ = [
    "CHURN_DEPTH",
    "EMPLOYEE_ID",
    "PROCEDURE_ID",
    "PROCEDURE_VERSION",
    "RECEIPT_SCHEMA",
    "DeepenCandidate",
    "DeepenOutcome",
    "apply_deepened",
    "build_prompt",
    "churn_counts",
    "deepen_output_check",
    "deepen_queue",
    "validate_deepen_staging",
]
