"""录制库:主测试缝的素材。

## 录制库该长什么样

**大多数录制应该是手写的最小化剧本,不是真实快照。** 一份录制只需要说清楚
"这一步产生了什么文件、写出了什么结果",不需要还原真实 Agent 的全部思考过程。
手写的剧本读起来是可读的测试意图;真实快照读起来是一堵墙。

只有需要验证"真实 Agent 的行为确实如此"时才用真录制(`record_job`)。

## 关于模型版本漂移

真实录制会随模型版本失真——同样的提示词,新版本可能产出不同的文件。缓解方式:

- 手写剧本不受影响,因为它们本来就不声称是真实输出;
- 真实录制入库前人工过一遍(真实输出里常带环境相关的绝对路径);
- 定期用一条打标记、默认跳过的测试跑真实 Agent 来检测漂移,而不是让常规 CI 承担。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentgenome.agents.artifacts import RESULT_FILENAME
from agentgenome.agents.runtime import JobResult, JobSpec

RESULT_FILE = RESULT_FILENAME
STREAM_FILE = "stream.jsonl"
META_FILE = "meta.yaml"
FILES_DIR = "files"
#: 产物目录里 result.json 之外的产物(staging 树等)。`files/` 是工作区相对,这一份
#: 是产物目录相对——产物目录带着任务号,录制没法用工作区相对路径指到它。
OUTPUTS_DIR = "outputs"


class RecordingNotFound(LookupError):
    """录制库里没有匹配这次 Job 的录制。

    **绝不静默返回空结果。** 静默降级会制造假绿测试,而假绿比没有测试更危险——
    一整条链路可能什么都没跑,而测试报告是绿的。
    """


def recording_key(employee_id: str, procedure_id: str, round_: int, subject: str = "") -> str:
    """录制目录名。自解释,不与调用顺序隐式耦合。

    **`subject` 是并行派发逼出来的那一维。** 逐模块深读会同时派几十个同 employee 同 procedure
    的作业,不带模块的话它们全撞在一个键上——回放会给每一个模块返回同一份产出,而测试照样
    是绿的:那正是"假绿比没有测试更危险"的教科书形态。

    不给 `subject` 时键与此前逐字一致,既有录制不必重录。
    """
    tail = f"__{subject}" if subject else ""
    return f"{employee_id}__{procedure_id}{tail}__r{round_}"


def session_key(employee_id: str, session_id: str, message_index: int) -> str:
    """会话回放的键。

    **与 Job 的键分开一个命名空间**(`__s__` 中缀),否则一个叫 `sess-1` 的会话与一个
    叫 `sess-1` 的工序会撞在一起——而撞了之后回放会安静地返回另一份产出。

    `message_index` 从 1 起,对应会话里的第几轮问答。这一维是会话版的 `round`:
    同一个会话的多轮必须返回不同结果,否则测不出"它记得上一句"。
    """
    return f"{employee_id}__s__{session_id}__m{message_index}"


@dataclass(frozen=True)
class Recording:
    """一份录制。"""

    path: Path

    @property
    def result_text(self) -> str | None:
        target = self.path / RESULT_FILE
        return target.read_text(encoding="utf-8") if target.is_file() else None

    @property
    def events(self) -> list[dict[str, Any]]:
        target = self.path / STREAM_FILE
        if not target.is_file():
            return []
        return [
            json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line
        ]

    @property
    def meta(self) -> dict[str, Any]:
        target = self.path / META_FILE
        if not target.is_file():
            return {}
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}

    def write_files_into(self, workdir: Path) -> list[str]:
        """把录制的文件按相对路径真实写入工作区。

        这是回放缝能成立的前提:写完之后 git diff、质量门禁、越权检查跑的都是真实
        产物,跟真实 Agent 干完活之后没有区别。
        """
        return self._write_tree(self.path / FILES_DIR, workdir)

    def write_outputs_into(self, output_dir: Path) -> list[str]:
        """把录制的产物目录文件(`outputs/`)写进这次 Job 的产物目录。

        树产出类工序(PRD 34)的产物是 `staging/` 下的真实文件,而产物目录带着
        任务号——录制没法用工作区相对路径指到它,所以单独一个相对产物目录的树。
        """
        return self._write_tree(self.path / OUTPUTS_DIR, output_dir)

    @staticmethod
    def _write_tree(source: Path, destination: Path) -> list[str]:
        if not source.is_dir():
            return []
        written = []
        for item in sorted(source.rglob("*")):
            if not item.is_file():
                continue
            relative = item.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            written.append(str(relative))
        return written


class RecordingLibrary:
    """按 `(employee, procedure, subject, round)` 索引的录制集合。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def keys(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(item.name for item in self.root.iterdir() if item.is_dir())

    def find_session(self, employee_id: str, session_id: str, message_index: int) -> Recording:
        """会话某一轮的录制。未命中同样直接抛——静默降级会制造假绿测试。"""
        return self._at(session_key(employee_id, session_id, message_index))

    def find(
        self, employee_id: str, procedure_id: str, round_: int, subject: str = ""
    ) -> Recording:
        return self._at(recording_key(employee_id, procedure_id, round_, subject))

    def _at(self, key: str) -> Recording:
        """按键取一份录制。**未命中直接抛,不静默降级**——见 `RecordingNotFound`。

        Job 与会话共用这一段:两处各写一遍的话,其中一处的错误信息迟早会少掉"当前有哪些"
        那一行,而那一行正是排查未命中时唯一有用的信息。
        """
        path = self.root / key
        if not path.is_dir():
            raise RecordingNotFound(
                f"录制库里没有 {key}(库位置: {self.root})。"
                f"当前有: {', '.join(self.keys()) or '(空)'}"
            )
        return Recording(path)


def changed_files(workdir: Path) -> set[str]:
    """本次运行改动过的文件,相对工作区。

    录制只收改动,不收整个工作区:在一个挂了业务子模块的 Workspace 上,后者会把
    整个仓库塞进录制目录。

    不是 git 仓时没有基线可比,退回到"收全部"——宁可多收也不要漏,录制不完整会在
    回放时表现成"产物凭空少了一个",比录制冗余难查得多。
    """
    from agentgenome.space.git_ws import working_tree_touched_paths
    from agentgenome.space.gitcmd import GitError

    try:
        touched = working_tree_touched_paths(workdir)
    except GitError:
        return {
            str(item.relative_to(workdir))
            for item in workdir.rglob("*")
            if item.is_file() and ".git" not in item.parts
        }
    return {path for path in touched if (workdir / path).is_file()}


def record_job(library_root: Path, spec: JobSpec, result: JobResult) -> Path:
    """把一次真实运行落盘成一份可直接回放的录制。

    产出**必须人工过一遍再入库**:真实输出里常带环境相关的绝对路径、临时目录名、
    以及与本次任务无关的噪声。
    """
    target = Path(library_root) / recording_key(
        spec.employee_id, spec.procedure_id, spec.round, spec.subject
    )
    target.mkdir(parents=True, exist_ok=True)

    if result.result_path and result.result_path.is_file():
        shutil.copy2(result.result_path, target / RESULT_FILE)
    if result.log_path and result.log_path.is_file():
        shutil.copy2(result.log_path, target / STREAM_FILE)

    files_root = target / FILES_DIR
    for relative in sorted(changed_files(spec.workdir)):
        source = spec.workdir / relative
        destination = files_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    # 产物目录里 result.json 之外的产物(staging 树等)收进 outputs/。上下文包与事件流
    # 不收:它们是过程记录,回放自己会重新生成,录进去只会在回放时把新鲜的盖成旧的。
    outputs_root = target / OUTPUTS_DIR
    skipped = {RESULT_FILE}
    for item in sorted(spec.output_dir.rglob("*")):
        if not item.is_file():
            continue
        relative_path = item.relative_to(spec.output_dir)
        name = relative_path.parts[0]
        if name in skipped or name.startswith("context-") or name.startswith("job-attempt-"):
            continue
        destination = outputs_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)

    meta = {
        "recorded_from": {
            "task_id": spec.task_id,
            "employee_id": spec.employee_id,
            "procedure": spec.procedure_ref,
            "round": spec.round,
        },
        "tokens_used": result.tokens_used if result.tokens_available else None,
        "note": "真实运行的录制。入库前请人工检查是否带了环境相关的绝对路径。",
    }
    (target / META_FILE).write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False))
    return target


__all__ = [
    "OUTPUTS_DIR",
    "Recording",
    "changed_files",
    "RecordingLibrary",
    "RecordingNotFound",
    "record_job",
    "recording_key",
]
