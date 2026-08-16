"""按平台真实约定的传输实现:MinIO 任务目录 + Matrix 通知 + 轮询。

源码分析(`.scratch/agentteams-runtime/source-analysis.md`)的结论:AgentTeams
没有任务 API,派发是文件 + 消息约定。这个实现直接说那套约定的母语:

1. 渲染任务目录(`taskdoc`)并经 `mc`(`mirror`)推送;
2. 往 Worker 的 Matrix 房间发 @mention;
3. 轮询 `meta.json` 直到终态;
4. 拉回工作区与产物,差分装配成传输应答。

超时不在这里管——轮询会一直转,适配器的墙钟超时(`JobSpec.timeout_s`)负责
掐断整个 `run_job`。用量恒为不可得:平台没有逐任务计量,如实标注。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import urllib.parse
import uuid
from pathlib import Path

from agentgenome.agents.agentteams.directory import WorkerDirectory
from agentgenome.agents.agentteams.http import http_json
from agentgenome.agents.agentteams.mirror import MinioMirror
from agentgenome.agents.agentteams.taskdoc import (
    build_outcome,
    normalize_artifact_paths,
    parse_meta_status,
    render_meta,
    render_spec,
    task_ref,
)
from agentgenome.agents.agentteams.transport import TransportJob, TransportOutcome
from agentgenome.agents.agentteams.workspace import ensure_inside, snapshot_workspace

#: 轮询 meta.json 的间隔。任务分钟级,秒级轮询已经足够灵敏。
POLL_INTERVAL_S = 5.0


class MatrixMinioTransport:
    """一条按文件 + 消息约定走的通道。全部令牌都是构造参数,进不了录制素材。"""

    def __init__(
        self,
        *,
        controller_endpoint: str,
        controller_token: str,
        matrix_homeserver: str,
        matrix_token: str,
        directory: WorkerDirectory,
        mirror: MinioMirror,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._controller = controller_endpoint.rstrip("/")
        self._controller_token = controller_token
        self._homeserver = matrix_homeserver.rstrip("/")
        self._matrix_token = matrix_token
        #: 员工 → Worker/房间。**每个 Job 按员工解析**,不是构造时定死一对——
        #: 定死的话所有员工挤在一个容器里,角色隔离在容器这一侧就不成立。
        self._directory = directory
        self._mirror = mirror
        self._poll_interval_s = poll_interval_s

    def preflight(self) -> None:
        """三样都要活:mc 客户端、controller、Matrix 令牌。启动期暴露,不留到派发。"""
        self._mirror.preflight()
        http_json("GET", f"{self._controller}/api/v1/status", self._controller_token)
        http_json("GET", f"{self._homeserver}/_matrix/client/v3/account/whoami", self._matrix_token)

    async def run_job(self, job: TransportJob) -> TransportOutcome:
        ref = task_ref(job)
        pushed = dict(job.workspace or {})
        # 断点续接——真机跑出来的教训:编排器进程崩溃重启后重派同一作业,
        # 重推会把 Worker 已完成/进行中的现场抹掉,而重知会会被 Matrix 的
        # txn 去重吞掉,Worker 永远不会再被叫醒。所以:现场已在就不重推;
        # 只有非终态(含"推完没来得及知会"的崩溃窗口)才补一次知会。
        existing = await self._mirror.read_file(f"tasks/{ref}/meta.json")
        if existing is None:
            await self._push_task(job, ref, pushed)
        terminal_already = existing is not None and parse_meta_status(existing)[1]
        if not terminal_already:
            await self._notify_worker(job, ref)
        status = await self._wait_terminal(ref)
        # 沉降一拍再回收:Worker 侧的 mc mirror 不保证上传顺序,meta 先落、
        # 产物后到是真实会发生的形态。等一个轮询间隔能挡住大部分半同步现场;
        # 彻底的顺序保证要靠真机验证阶段确认 Worker 侧行为。
        await asyncio.sleep(self._poll_interval_s)
        return await self._collect(ref, status, pushed, subject=job.subject)

    # --- 派发 ----------------------------------------------------------------

    async def _push_task(self, job: TransportJob, ref: str, pushed: dict[str, str]) -> None:
        """把任务目录整体推上去。spec/meta/workspace 一次镜像,现场以推送方为准。"""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "spec.md").write_text(render_spec(job), encoding="utf-8")
            (root / "meta.json").write_text(
                json.dumps(render_meta(job), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            workspace = root / "workspace"
            workspace.mkdir()
            workspace_root = workspace.resolve()
            for relative, content in pushed.items():
                # 推送侧与拉回侧共用同一道越界闸:快照正常来自本地工作区,
                # 但回放素材经 from_dict 进来的键不受那条路约束。
                target = ensure_inside(workspace_root, relative, "任务工作区路径")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            await self._mirror.push_dir(root, f"tasks/{ref}")

    async def _notify_worker(self, job: TransportJob, ref: str) -> None:
        """Matrix @mention。txn 前缀带任务引用求可追溯,**尾部每次唯一**:
        续接补发的知会不能撞上崩溃前那次的 txn——homeserver 的去重会静默
        吞掉它,表现为"Worker 就是不动",极难归因。"""
        worker = await self._directory.locate(job.employee_id)
        room = urllib.parse.quote(worker.room_id, safe="")
        txn = urllib.parse.quote(f"agentgenome-{ref}-{uuid.uuid4().hex[:8]}", safe="")
        body = (
            f"@{worker.name} New task [{ref}] from {job.employee_id}"
            f"({job.procedure_ref}). Run file-sync to pull "
            f"tasks/{ref}/ and follow spec.md."
        )
        await asyncio.to_thread(
            http_json,
            "PUT",
            f"{self._homeserver}/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}",
            self._matrix_token,
            {"msgtype": "m.text", "body": body},
        )

    # --- 等待与回收 -----------------------------------------------------------

    async def _wait_terminal(self, ref: str) -> str:
        """轮询到终态并**把状态带回去**——回收阶段不再重读 meta:重读遇上
        瞬时缺失会把一个已经终结的任务错报成 UNKNOWN 失败。"""
        while True:
            meta = await self._mirror.read_file(f"tasks/{ref}/meta.json")
            if meta is not None:
                status, terminal = parse_meta_status(meta)
                if terminal:
                    return status
            await asyncio.sleep(self._poll_interval_s)

    async def _collect(
        self, ref: str, status: str, pushed: dict[str, str], *, subject: str
    ) -> TransportOutcome:
        result_md = await self._mirror.read_file(f"tasks/{ref}/result.md") or ""
        with tempfile.TemporaryDirectory() as scratch:
            artifacts_dir = Path(scratch) / "artifacts"
            legacy_staging_dir = Path(scratch) / "legacy-staging"
            workspace_dir = Path(scratch) / "workspace"
            await self._mirror.pull_dir(f"tasks/{ref}/artifacts", artifacts_dir)
            # PRD 34 引入 staging 树之前，远端只有 artifacts/ 这一条产物通道；
            # 后来工序提示里的“产物目录下 staging/”被旧 Worker 解释成了任务根
            # staging/。本地运行时的真相始终是 output_dir/staging/，所以回收时把
            # 这份历史形态迁回同一个命名空间。canonical artifacts/staging/ 优先，
            # 防止一个残留的旧副本覆盖 Worker 按新约定交回的内容。
            await self._mirror.pull_dir(f"tasks/{ref}/staging", legacy_staging_dir)
            await self._mirror.pull_dir(f"tasks/{ref}/workspace", workspace_dir)
            # 复用工作区快照的过滤规则(.git、符号链接、非 UTF-8)——拉回侧与
            # 推送侧对"什么算现场"必须是同一个答案。
            artifacts = {
                f"staging/{name}": content
                for name, content in snapshot_workspace(legacy_staging_dir).items()
            }
            raw_artifacts = snapshot_workspace(artifacts_dir)
            artifacts.update(normalize_artifact_paths(raw_artifacts, subject))
            pulled = snapshot_workspace(workspace_dir)
        outcome = build_outcome(
            status=status,
            result_md=result_md,
            artifacts=artifacts,
            pushed=pushed,
            pulled=pulled,
        )
        corrected = sorted(set(raw_artifacts) - set(artifacts))
        if corrected:
            outcome.events.append(
                {
                    "kind": "note",
                    "text": "AgentTeams 纠正了重复产物槽前缀: " + ", ".join(corrected),
                }
            )
        return outcome


__all__ = ["MatrixMinioTransport", "POLL_INTERVAL_S"]
