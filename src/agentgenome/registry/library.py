"""全局基因库:模板、通用 Procedure、跨项目经验。

## 拉来的东西是起点不是终点

模板生成的规则、拉取的 Procedure,都会被项目自己的进化管道继续打磨。**全局库提供的是不用从零
开始的起跑线,不是一份必须照做的标准**——把它当标准的话,每个项目的基因组会停在同一个地方。

## 上交的审批标准必须更严

全局基因库的**污染半径是所有项目**。项目内的一条错卡片只坑一个项目;全局库里的一条错卡片
会被每个新项目继承。判据是"这条经验是否与具体项目无关"——只要跟某个项目的特定架构、特定
业务绑定,就不该进全局库。

## 命中数是上交的硬门槛

**一条从没帮上过忙的经验,没有理由推给所有人。** 命中为零就是"它在源项目都没被验证过",
而未经验证的经验正是污染的主要来源。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentgenome import paths
from agentgenome.genome.evolution.cards import LessonCard

#: 基因库里的三个区。
TEMPLATES_DIR = "templates"
PROCEDURES_DIR = "procedures"
LESSONS_DIR = "lessons"

#: 模板的元数据文件。
TEMPLATE_MANIFEST = "template.yaml"

#: 看起来像"绑定了某个具体项目"的痕迹。命中即警告。两个信号,互补:
#:
#: 1. **具体的挂载路径**(`repos/<某个仓>`)。这是最强的一条,因为它**按构造**就是项目特有
#:    的——挂载点直接印着本项目的仓库名。刻意只抓带具体仓名的形态:光一个 `repos/**` 是
#:    这套约定本身、每个项目都长这样,抓它只会把"权限范围是 repos/** 时要注意…"这类真·
#:    通用经验误标。
#: 2. **命名习惯启发式**。抓的是"正文里直接点了模块名但没带路径"的情形,路径信号覆盖不到。
#:
#: 位置编号形态(`code-N`)的那条已经删了——挂载点语义化之后它再也不会出现,留着是一条
#: 永远匹配不上的死规则,而死规则会让这道门看起来比实际更严。
_PROJECT_SPECIFIC = re.compile(
    rf"\b{re.escape(paths.REPOS.as_posix())}/(?!\*)[A-Za-z0-9._-]+|\b[a-z]+-service\b"
)


class RegistryUnavailable(FileNotFoundError):
    """基因库不在。"""


@dataclass(frozen=True)
class Template:
    """一份项目基因组骨架。"""

    name: str
    summary: str
    path: Path
    #: 适用的项目形态:python-service / frontend-monorepo / multi-module …
    shape: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "summary": self.summary, "shape": self.shape}


@dataclass(frozen=True)
class Provenance:
    """拉来的东西从哪儿来。

    **必须记。** 三个月后有人问"这条规则为什么长这样",唯一能顺藤摸瓜的入口就是它;而一个
    没有来源的资产会被当成本项目自己写的,于是没人敢动它。
    """

    registry: str
    kind: str
    name: str
    revision: str = ""

    def as_comment(self) -> str:
        rev = f" @ {self.revision}" if self.revision else ""
        return (
            f"# 来自全局基因库 {self.registry}{rev} 的 {self.kind}/{self.name}\n"
            "# 它是起点不是终点——本项目的进化管道会继续打磨它。\n"
        )


@dataclass
class Registry:
    """一个本地可访问的基因库(通常是一个 clone 下来的 Git 仓)。"""

    root: Path
    name: str = "global"

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def ensure(self) -> None:
        if not self.root.is_dir():
            raise RegistryUnavailable(
                f"基因库不在: {self.root}。它是一个普通 Git 仓库,先 clone 下来再配地址。"
            )

    # --- 模板 ---------------------------------------------------------------

    def templates(self) -> list[Template]:
        found: list[Template] = []
        base = self.root / TEMPLATES_DIR
        if not base.is_dir():
            return found
        for directory in sorted(base.iterdir()):
            manifest = directory / TEMPLATE_MANIFEST
            if not manifest.is_file():
                continue
            payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            found.append(
                Template(
                    name=directory.name,
                    summary=str(payload.get("summary", "")),
                    shape=str(payload.get("shape", "")),
                    path=directory,
                )
            )
        return found

    def template(self, name: str) -> Template:
        for item in self.templates():
            if item.name == name:
                return item
        known = ", ".join(item.name for item in self.templates()) or "(空)"
        raise KeyError(f"基因库里没有模板 {name}(有: {known})")

    def apply_template(self, name: str, workspace_root: Path) -> list[str]:
        """把一份模板铺进 Workspace。

        **已存在的文件不覆盖。** 模板是起跑线,不是重置按钮——一次误用不该抹掉项目已经积累
        的认知。
        """
        template = self.template(name)
        written: list[str] = []
        source = template.path / "genome"
        if not source.is_dir():
            return written
        for item in sorted(source.rglob("*")):
            if not item.is_file() or item.name == TEMPLATE_MANIFEST:
                continue
            relative = item.relative_to(source)
            target = Path(workspace_root) / "genome" / relative
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            body = item.read_text(encoding="utf-8")
            head = (
                Provenance(self.name, "template", name).as_comment() if _commentable(target) else ""
            )
            target.write_text(head + body, encoding="utf-8")
            written.append(str(relative))
        return written

    # --- Procedure --------------------------------------------------------------

    def procedures(self) -> list[str]:
        base = self.root / PROCEDURES_DIR
        return (
            sorted(item.name for item in base.iterdir() if item.is_dir()) if base.is_dir() else []
        )

    def pull_procedure(self, procedure_id: str, workspace_root: Path) -> Path:
        source = self.root / PROCEDURES_DIR / procedure_id
        if not source.is_dir():
            raise KeyError(
                f"基因库里没有工序 {procedure_id}"
                f"(有: {', '.join(self.procedures()) or '(空)'})"
            )
        target = Path(workspace_root) / "genome" / "procedures" / procedure_id
        if target.exists():
            raise FileExistsError(
                f"{procedure_id} 在这个项目里已经有了。要换成基因库的版本,先删掉本地那份——"
                "静默覆盖会把项目自己的改动抹掉。"
            )
        shutil.copytree(source, target)
        (target / "PROVENANCE").write_text(
            Provenance(self.name, "procedure", procedure_id).as_comment(), encoding="utf-8"
        )
        return target


@dataclass(frozen=True)
class ContributionCheck:
    """一次上交的审查结果。"""

    allowed: bool
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


def check_contribution(card: LessonCard, force: bool = False) -> ContributionCheck:
    """这条经验够不够格进全局库。

    **标准比项目内更严**,因为污染半径是所有项目。两道:

    1. **命中数为零直接拒。** 一条从没帮上过忙的经验没有理由推给所有人;
    2. **看起来绑定了具体项目就警告。** 出现 `repos/order-service` 这类具体挂载路径、
       或者直接点了模块名,大概率是把本项目的特定架构写进了"通用经验"。

    第二道只警告不拒绝:判断"是不是真的通用"最终要靠人,而误拒会让真正有价值的经验上不来。
    """
    reasons = []
    if card.hits <= 0 and not force:
        reasons.append(
            f"{card.id} 在源项目命中 0 次。未经验证的经验是全局库污染的主要来源——"
            "确实要上交的话用 --force 并说明理由。"
        )

    warnings = []
    body = f"{card.title}\n{card.conclusion}\n{' '.join(card.applies_to.modules)}"
    hits = sorted(set(_PROJECT_SPECIFIC.findall(body)))
    if hits:
        warnings.append(
            f"正文里出现了看起来项目特有的名字({', '.join(hits)})。"
            "全局库的判据是「与具体项目无关」——确认它不是本项目的特定架构。"
        )
    if card.applies_to.modules:
        warnings.append("适用条件点名了模块 id,而模块 id 在别的项目里不存在。")

    return ContributionCheck(allowed=not reasons, reasons=tuple(reasons), warnings=tuple(warnings))


def _commentable(path: Path) -> bool:
    """能不能安全地加一行 `#` 注释。JSON 不行——加了就不是合法 JSON 了。"""
    return path.suffix in {".md", ".yaml", ".yml", ".toml", ".py"}


__all__ = [
    "LESSONS_DIR",
    "PROCEDURES_DIR",
    "TEMPLATES_DIR",
    "ContributionCheck",
    "Provenance",
    "Registry",
    "RegistryUnavailable",
    "Template",
    "check_contribution",
]
