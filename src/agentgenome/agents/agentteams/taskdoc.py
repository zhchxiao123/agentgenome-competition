"""MatrixMinio 传输的纯逻辑层:任务文档、状态映射、工作区差分。

AgentTeams 的任务派发是文件 + 消息约定,不是 API(见
`.scratch/agentteams-runtime/source-analysis.md`)。这里把那套约定收敛成纯函数:
渲染要推送的任务目录内容、解析拉回的状态、把两次快照的差分装配成传输应答。
**不碰网络与磁盘**——I/O 在存储驱动与传输装配层,这里的每一行都可以直接断言。

## 任务目录布局(我们写的 / Worker 写的)

    shared/tasks/{ref}/spec.md              ← 我们:上下文 + 手艺 + 交付约定
    shared/tasks/{ref}/meta.json            ← 我们置 PENDING;Worker 更新状态
    shared/tasks/{ref}/workspace/**         ← 我们推快照;Worker 就地改
    shared/tasks/{ref}/result.md            ← Worker:人类可读小结(进日志面)
    shared/tasks/{ref}/artifacts/**          ← Worker:本地 output_dir 的完整镜像
      ├── result.json                       ← 结构化小票(进契约校验)
      └── staging/**                        ← 文件型产物(知识树、经验卡片等)
    shared/tasks/{ref}/staging/**            ← 旧 Worker 形态,回收时兼容迁入 artifacts/
"""

from __future__ import annotations

import json
from typing import Any

from agentgenome.agents.agentteams.transport import TransportJob, TransportOutcome

#: 上游 copaw 的终态枚举(task.py)。进行中的状态不在这份清单里——
#: 不认识的状态一律按进行中处理,下一轮再看,而不是把半写的 meta 判成结束。
TERMINAL_STATUSES = frozenset(
    {"SUCCESS", "SUCCESS_WITH_NOTES", "REVISION_NEEDED", "BLOCKED", "INTERRUPTED"}
)
#: 算成功的那两个。
EFFECTIVE_STATUSES = frozenset({"SUCCESS", "SUCCESS_WITH_NOTES"})


def task_ref(job: TransportJob) -> str:
    """这个 Job 在远端的任务目录名。

    **员工与工序都必须在引用里。** 工序那一维是真机跑出来的教训:计划与开发同任务
    同轮,引用不含工序时共用远端目录,断点续接会把上一道工序的产物当成这一道的。
    员工那一维在按员工分容器之后变得更危险——同一道工序若由两个员工分别执行,
    撞名意味着**两个不同的容器往同一个远端目录里写**,而它们互相看不见对方。
    重试与并行 subject 同理,各有目录。

    subject 压平成一段路径后**附短哈希**:单纯把 `/` 换成 `-` 不是单射
    (`a/b` 与 `a-b` 会撞),哈希把唯一性从"碰巧不撞"变成结构保证,
    可读前缀留给人眼。
    """
    import hashlib

    procedure_id = job.procedure_ref.split("@", 1)[0]
    ref = f"{job.task_id}-{job.employee_id}-{procedure_id}-r{job.round}"
    if job.subject:
        digest = hashlib.sha1(job.subject.encode("utf-8")).hexdigest()[:6]
        ref += "-" + job.subject.replace("/", "-") + f"-{digest}"
    if job.attempt > 1:
        ref += f"-a{job.attempt}"
    return ref


def render_spec(job: TransportJob) -> str:
    """spec.md:Worker 看到的全部指令。

    工具边界如实写成**劝导**——平台没有逐任务工具限制机制(源码分析第 7 条),
    把它写成"系统会拦你"是在撒谎,Worker 撞上时的行为会不可预期。
    """
    parts = [job.context_text.rstrip(), ""]
    if job.craft:
        parts += [job.craft.rstrip(), ""]
    parts += [
        "## 工作区与交付约定",
        "",
        "- 代码改动:直接修改本任务目录 `workspace/` 下的文件,不要动目录之外的东西。",
        "- 产物目录是 `artifacts/`;它已经对应本地 Job 的 `output_dir`,",
        "  不要把上下文里的 `tasks/<任务>/artifacts/<槽位>/` 再复制进来。",
        "- 工序要求产出 `staging/x` 时,远端路径就是 `artifacts/staging/x`;",
        "- 例如工序要求 `staging/lessons/x.md` 时,远端路径就是",
        "  `artifacts/staging/lessons/x.md`,不要写到任务根的 `staging/`,",
        "  绝不能写成 `artifacts/<模块或槽位>/staging/x`。",
        "- 结构化结果:写到产物目录的 `result.json`,即 `artifacts/result.json`。",
        "- 人类可读小结:写到本任务目录 `result.md`。",
        "- 先写完并同步全部工作区与产物,最后才把 `meta.json` 的 status 置为终态;",
        f"  成功用 SUCCESS,任务引用: `{task_ref(job)}`。",
    ]
    advice = []
    if job.tools_allow:
        advice.append(f"- 只应使用这些能力: {', '.join(job.tools_allow)}。")
    if job.tools_deny:
        advice.append(f"- 不要使用这些能力: {', '.join(job.tools_deny)}。")
    if advice:
        parts += [
            "",
            "## 工具边界(约定,非强制)",
            "",
            *advice,
        ]
    parts += ["", f"(token 预算参考上限: {job.max_tokens})", ""]
    return "\n".join(parts)


def render_meta(job: TransportJob) -> dict[str, Any]:
    """初始 meta.json。最小字段——与上游 TaskMeta 的严格对齐留到真机验证。"""
    return {
        "task_id": task_ref(job),
        "status": "PENDING",
        "employee_id": job.employee_id,
        "procedure_ref": job.procedure_ref,
    }


def parse_meta_status(text: str) -> tuple[str, bool]:
    """meta.json 文本 → (状态, 是否终态)。

    解析失败按进行中处理:mc 同步不是原子的,读到写了一半的 meta 是常态——
    把它判成失败会让一个正常运行的任务被误杀。
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return "UNPARSEABLE", False
    status = str(payload.get("status", "")) if isinstance(payload, dict) else ""
    return status or "UNKNOWN", status in TERMINAL_STATUSES


#: 任务面文件:我们的任务目录约定里住在 workspace **之外**的东西。
#: Worker(尤其 Hermes)习惯把它们也复制一份进 workspace 根——那是习惯不是
#: 代码改动,当成现场改动的话,一个只读工序会因此被判越权(真机实测撞上)。
_TASK_PLANE_TOP = frozenset({"result.md", "plan.md", "meta.json", "spec.md"})
_TASK_PLANE_DIRS = ("progress/", "artifacts/")


def strip_task_plane(pulled: dict[str, str]) -> dict[str, str]:
    """把误入 workspace 顶层的任务面文件滤掉。只滤顶层——嵌套同名是代码。"""
    return {
        path: content
        for path, content in pulled.items()
        if path not in _TASK_PLANE_TOP and not path.startswith(_TASK_PLANE_DIRS)
    }


def diff_workspace(pushed: dict[str, str], pulled: dict[str, str]) -> dict[str, str | None]:
    """推送快照 vs 拉回快照 → 改动集(值 `None` 表示删除)。

    形状与传输契约的 `changed_files` 一致,适配器那侧的事务性落地不用知道
    改动是差分出来的还是平台报的。
    """
    changed: dict[str, str | None] = {
        path: content for path, content in pulled.items() if pushed.get(path) != content
    }
    changed.update({path: None for path in pushed if path not in pulled})
    return changed


def normalize_artifact_paths(artifacts: dict[str, str], subject: str) -> dict[str, str]:
    """兼容 Worker 把本地产物槽重复套进远端 ``artifacts/`` 的已知形态。

    远端 ``artifacts/`` 已经对应本地 ``output_dir``。真机上 Worker 曾把上下文里的
    ``.../artifacts/<module>/staging`` 直译成 ``artifacts/<module>/staging``，导致
    本地契约看不到 canonical ``staging/``。这里只迁移当前 subject 的 staging 树：
    不猜任意首层目录，不碰普通产物；canonical 文件存在时始终以它为准。
    """
    if not subject or "/" in subject or subject in {".", ".."}:
        return dict(artifacts)

    misplaced_prefix = f"{subject}/staging/"
    recovered = {
        f"staging/{path.removeprefix(misplaced_prefix)}": content
        for path, content in artifacts.items()
        if path.startswith(misplaced_prefix)
    }
    canonical = {
        path: content
        for path, content in artifacts.items()
        if not path.startswith(misplaced_prefix)
    }
    recovered.update(canonical)
    return recovered


def build_outcome(
    status: str,
    result_md: str,
    artifacts: dict[str, str],
    pushed: dict[str, str],
    pulled: dict[str, str],
) -> TransportOutcome:
    """终态 + 拉回的现场 → 传输应答。

    用量恒为不可得:平台没有逐任务计量(源码分析第 5 条)。不填 0——
    填 0 会让成本看板悄悄少算,而"标不可得"正是契约里为此留的路。
    """
    ok = status in EFFECTIVE_STATUSES
    detail = None
    if not ok:
        summary = result_md.strip().splitlines()[0] if result_md.strip() else "(无 result.md)"
        detail = f"Worker 以 {status} 收场: {summary}"
    events = []
    if result_md.strip():
        events.append({"kind": "text", "text": result_md.strip()})
    return TransportOutcome(
        ok=ok,
        detail=detail,
        artifacts=dict(artifacts),
        changed_files=diff_workspace(pushed, strip_task_plane(pulled)),
        tokens_used=None,
        events=events,
    )


__all__ = [
    "EFFECTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "build_outcome",
    "diff_workspace",
    "normalize_artifact_paths",
    "parse_meta_status",
    "render_meta",
    "render_spec",
    "strip_task_plane",
    "task_ref",
]
