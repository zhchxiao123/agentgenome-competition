"""会话的 REST 面。

## 这条 SSE 与 `/events/stream` 不是一回事

`/events/stream` 刻意只报"有变化了"不带数据,因为页面数据随时可以重新拉。会话消息不适用
——**token 流本身就是交付物**。解法不是增量协议,而是**让流成为持久日志的 tail**:块边
生成边落盘,SSE 只是尾随,断线后带最后收到的序号走 `GET /sessions/{id}/messages?after=`
补齐。与任务日志"历史走分页 REST、实时走 SSE tail"是同一套打法。

## 读写权限没有 PATCH

翻遍这个模块也找不到一个能改已有会话 `writable` 的端点——那是故意的。只读与可写是两套工具集,
提供这个端点等于提供一条不经任何闸门的提权路径。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from agentgenome.agents.factory import RuntimeAssemblyError, build_session_runtimes, why_no_session
from agentgenome.config import load_config
from agentgenome.employees import EmployeeNotFound, load_employees, workspace_employees_root
from agentgenome.server.models import (
    BlockItem,
    BlockPage,
    EmployeeList,
    EmployeeSummary,
    FeedbackRequest,
    FeedbackResponse,
    InjectRequest,
    PinRequest,
    SessionCreateRequest,
    SessionList,
    SessionMessageRequest,
    SessionSummary,
    TaskDraft,
)
from agentgenome.sessions import log
from agentgenome.sessions.model import Session, SessionState
from agentgenome.sessions.service import SessionRefused, SessionService, SessionsUnsupported
from agentgenome.sessions.store import SessionNotFound, SessionStore


def mount_sessions(app: FastAPI) -> None:
    @app.post("/sessions", response_model=SessionSummary, status_code=201, tags=["sessions"])
    async def create_session(request: Request, body: SessionCreateRequest) -> SessionSummary:
        root = _root(request)
        service = _service(request, root)
        try:
            employee = load_employees(workspace_employees_root(root)).get(body.employee)
        except EmployeeNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        writable = _writable(body)
        try:
            session = await service.create(
                employee=employee,
                writable=writable,
                title=body.title,
                task_id=body.task_id,
                task_worktree=_task_worktree(root, writable, body.task_id),
                max_tokens=_session_budget(root),
            )
        except SessionRefused as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except SessionsUnsupported as error:
            # **两种成因给两句话。** 注册表只装支持会话的运行时,所以查不到既可能是"根本
            # 没配",也可能是"配了但开不了会话"——前者要改根配置,后者要换员工。合成一句
            # 的代价是用户按提示去改了不该改的地方,改完当然没用,而错误信息一个字没变。
            detail = why_no_session(load_config(root), employee.runtime)
            raise HTTPException(status_code=409, detail=detail) from error
        return SessionSummary.of(session)

    @app.get("/employees", response_model=EmployeeList, tags=["sessions"])
    async def list_employees(request: Request) -> EmployeeList:
        """有哪些员工可选,以及**哪些开得了会话**。

        兑现的是 PRD 27 那句"在 preflight 阶段暴露,而不是等用户创建会话时才失败"——只是
        暴露的位置从命令行的 preflight 挪到了控制面,因为界面上的选择发生在这里。没有它,
        前端只能硬编码员工 id,而"这个员工能不能开会话"要等提交之后才知道。
        """
        root = _root(request)
        config = load_config(root)
        registry = load_employees(workspace_employees_root(root), strict=False)
        items = []
        for employee in registry.all():
            can = employee.runtime in build_session_runtimes(config)
            items.append(
                EmployeeSummary(
                    id=employee.id,
                    name=employee.display_name,
                    runtime=employee.runtime,
                    can_session=can,
                    session_blocked_reason="" if can else why_no_session(config, employee.runtime),
                )
            )
        return EmployeeList(items=items)

    @app.get("/sessions", response_model=SessionList, tags=["sessions"])
    async def list_sessions(
        request: Request,
        writable: bool | None = Query(default=None),
        state: str | None = Query(default=None),
        task_id: str | None = Query(default=None),
    ) -> SessionList:
        root = _root(request)
        service = _service(request, root)
        store = SessionStore(root)
        found = store.list(
            writable=writable,
            state=SessionState(state) if state else None,
            task_id=task_id,
        )
        # 计数**从全量算,不从筛选结果算**——筛选器上那些数字回答的是"别的状态有没有积压",
        # 拿筛完的结果去数,选中「进行中」之后别的三项会一起变成 0。
        everything = store.list()
        counts = {value.value: 0 for value in SessionState}
        for item in everything:
            counts[item.state.value] += 1
        return SessionList(
            items=[_summary(root, item, service) for item in found],
            counts=counts,
            total=len(everything),
        )

    @app.get("/sessions/{session_id}", response_model=SessionSummary, tags=["sessions"])
    async def get_session(request: Request, session_id: str) -> SessionSummary:
        root = _root(request)
        service = _service(request, root)
        return _summary(root, _get(root, session_id), service)

    @app.post("/sessions/{session_id}/messages", tags=["sessions"])
    async def send_message(
        request: Request, session_id: str, body: SessionMessageRequest
    ) -> StreamingResponse:
        """发一条消息,SSE 流式回块。

        这一轮在后台任务里跑(`SessionService.start_turn`),**不绑定这次连接**——这次
        请求关掉不会打断生成,已经在跑的那一轮会继续把块落盘。这次响应本身只是接上
        `SessionService.attach` 看直播,和重新打开页面时用的是同一条路(见
        `GET .../messages/stream`)。
        """
        root = _root(request)
        service = _service(request, root)
        # **先把拒绝判掉再开流。** 会话不存在、已挂起、或上一轮还没完时该回 404/409,
        # 而不是回一条 200 的空流——SSE 一旦开出去就没法改状态码了,前端只能看见
        # "什么都没来"。
        try:
            await service.ensure_open(session_id)
        except SessionNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except SessionRefused as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

        try:
            start_seq = service.start_turn(session_id, body.message)
        except SessionRefused as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

        async def stream() -> AsyncIterator[str]:
            async for block in service.attach(session_id, after=start_seq):
                yield f"data: {json.dumps(block.as_dict(), ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/sessions/{session_id}/messages", response_model=BlockPage, tags=["sessions"])
    async def message_history(
        request: Request,
        session_id: str,
        after: int = Query(default=0, ge=0, description="只要这个序号之后的块"),
    ) -> BlockPage:
        root = _root(request)
        service = _service(request, root)
        try:
            items = service.history(session_id, after=after)
        except SessionNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        blocks = [BlockItem(**_block(item)) for item in items]
        return BlockPage(
            session_id=session_id,
            items=blocks,
            last_seq=blocks[-1].seq if blocks else after,
        )

    @app.get("/sessions/{session_id}/messages/stream", tags=["sessions"])
    async def message_stream(
        request: Request,
        session_id: str,
        after: int = Query(default=0, ge=0, description="只要这个序号之后的块"),
    ) -> StreamingResponse:
        """接上一个会话的块流:补齐历史,如果这会儿有一轮在跑就跟着直播。

        **专给"重新打开页面/切回来发现上次那轮可能还没说完"用。** 发消息本身走
        `POST .../messages`,那条路自己也会开流看自己起的这一轮——这里不起新的一轮,
        只负责接,`SessionSummary.generating` 就是用来判断"要不要接"的那个字段。

        没有一轮在跑时,补完历史这条 SSE 就读完即止,不会挂着不放。
        """
        root = _root(request)
        service = _service(request, root)
        try:
            service.store.get(session_id)
        except SessionNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

        async def stream() -> AsyncIterator[str]:
            async for block in service.attach(session_id, after=after):
                yield f"data: {json.dumps(block.as_dict(), ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/sessions/{session_id}/stop", response_model=SessionSummary, tags=["sessions"])
    async def stop_turn(request: Request, session_id: str) -> SessionSummary:
        """打断正在跑的那一轮。没有一轮在跑时**不是错误**——用户点两下很正常,第二下
        它已经如愿了,不该报 404/409。
        """
        root = _root(request)
        service = _service(request, root)
        session = _get(root, session_id)
        service.stop_turn(session_id)
        return _summary(root, session, service)

    @app.post("/sessions/{session_id}/end", response_model=SessionSummary, tags=["sessions"])
    async def end_session(request: Request, session_id: str) -> SessionSummary:
        root = _root(request)
        return SessionSummary.of(_service(request, root).end(session_id))

    @app.post("/sessions/{session_id}/resume", response_model=SessionSummary, tags=["sessions"])
    async def resume_session(request: Request, session_id: str) -> SessionSummary:
        """恢复一个已挂起的会话。已结束的不行——那才是不可恢复的那一档。

        **上限按当前配置重取**,理由见 `SessionService.resume`:被预算挂起的会话不重取的话,
        恢复之后下一轮结账时会原地再挂一次。
        """
        root = _root(request)
        try:
            return SessionSummary.of(
                _service(request, root).resume(session_id, max_tokens=_session_budget(root))
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/sessions/{session_id}/pin", response_model=SessionSummary, tags=["sessions"])
    async def pin_context(request: Request, session_id: str, body: PinRequest) -> SessionSummary:
        """钉住/取消钉住一条上下文。钉住的不参与截断。"""
        root = _root(request)
        return SessionSummary.of(_service(request, root).pin(session_id, body.item, body.pinned))

    @app.delete(
        "/sessions/{session_id}/context/{item:path}",
        response_model=SessionSummary,
        tags=["sessions"],
    )
    async def drop_context(request: Request, session_id: str, item: str) -> SessionSummary:
        """把一条上下文从这次会话里移掉——员工带偏了时用户要能把噪声拿掉。"""
        root = _root(request)
        return SessionSummary.of(_service(request, root).drop_context(session_id, item))

    @app.post(
        "/sessions/{session_id}/feedback", response_model=FeedbackResponse, tags=["sessions"]
    )
    async def feedback(
        request: Request, session_id: str, body: FeedbackRequest
    ) -> FeedbackResponse:
        """有用/没用。**这是「对话成为知识自然选择的输入源」唯一的落地点**(§10.2)。

        记账在后端做,前端不自行改任何 hits 显示值——那是基因组的写操作。
        """
        root = _root(request)
        try:
            credited = _service(request, root).feedback(session_id, body.useful)
        except SessionNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FeedbackResponse(credited=list(credited))

    @app.post("/sessions/{session_id}/escalate", response_model=TaskDraft, tags=["sessions"])
    async def escalate(request: Request, session_id: str) -> TaskDraft:
        """会话转任务:**返回草稿,不建任务**。

        自动建任务会让"随口问一句"变成"莫名多了个任务",而一次轻量提问的全部价值就在于它够轻。

        **任何会话都能转。** 早先这里只放行咨询,那是三选一模式下的产物;现在只读会话转出来
        的是一份需求草稿,可写会话转出来的草稿还带着它那条 `session/<id>` 分支——那正是可写
        会话**唯一**的出口:改动要进主线,只能由一个任务接管后走门禁与审批。
        """
        root = _root(request)
        session = _get(root, session_id)
        from agentgenome.sessions.drafts import draft_from

        service = _service(request, root)
        return TaskDraft(**draft_from(session, service.history(session_id)))

    @app.post("/sessions/{session_id}/inject", response_model=SessionSummary, tags=["sessions"])
    async def inject(request: Request, session_id: str, body: InjectRequest) -> SessionSummary:
        """质询结论回注为审批/驳回意见。

        **审批人身份仍由服务端二次校验**,会话不构成身份旁路——能问不等于能批。
        """
        root = _root(request)
        session = _get(root, session_id)
        # 准入只看"关没关任务"——回注的是**对某个任务的**意见,没有任务就没有回注对象。
        # 读写权限与这件事无关:可写会话同样可以在改完之后给出一条审批意见。
        if not session.task_id:
            raise HTTPException(status_code=400, detail="只有关联了任务的会话能回注意见")
        from agentgenome.sessions.drafts import record_injection

        record_injection(root, session, body.decision, body.comment)
        return SessionSummary.of(session)


# --- 内部 ---------------------------------------------------------------------


#: 旧模式名 → 能不能改代码。**只为兼容而存在**,见 `SessionCreateRequest.mode`。
_LEGACY_MODES = {"consult": False, "inquiry": False, "pair": True}


def _writable(body: SessionCreateRequest) -> bool:
    """这次要建的是不是可写会话。

    `mode` 是过渡期的旧参数。两个都给且**矛盾**时报 400,不静默挑一个——静默挑的那次会让
    调用方以为自己传的参数生效了,而它其实被丢掉了。两个都给但一致时放行:那是调用方正在
    迁移的正常形态,没有理由拦。
    """
    if body.mode is None:
        return body.writable
    if body.mode not in _LEGACY_MODES:
        allowed = ", ".join(_LEGACY_MODES)
        raise HTTPException(
            status_code=400, detail=f"未知模式 {body.mode}(允许: {allowed})"
        )
    legacy = _LEGACY_MODES[body.mode]
    if body.writable and not legacy:
        raise HTTPException(
            status_code=400,
            detail=f"writable=true 与 mode={body.mode}(只读)矛盾。mode 已废弃,只传 writable。",
        )
    return legacy


def _session_budget(root: Path) -> int:
    """这个项目给一次会话的 token 上限。**0 = 不限,而且是缺省。**

    **每次现读,不缓存。** 会话服务是按工作区缓存的(它握着运行时句柄),把配置一起缓存
    进去的话,用户在配置面板上改完得重启服务才生效——而他改它的那一刻,通常正卡在一个
    被预算挂起的会话上。
    """
    return load_config(root).budgets.session_tokens


def _task_worktree(root: Path, writable: bool, task_id: str | None) -> Path | None:
    """可写会话关联了任务时,要跑在那个任务的隔离工作区里。

    **只有"可写 + 关了任务"这一种组合需要它。** 只读会话跑在项目根上,可写但没关任务的
    会话跑在自己的 worktree 里(`SessionService` 现开),两者都不该拿到一个任务的工作区
    ——给了只会让会话跟着那个 worktree 一起被清理。

    这里此前写死 `None`,结果是 `workdir_for` 对结对直接抛——**结对会话通过 API 根本
    建不出来**,而后端那一整套(越权检查、`finish_pair`、全链路)只有直接调编排器的测试
    在走。逻辑齐全但路径不通,比缺一块更难发现。
    """
    if not writable or not task_id:
        return None

    from agentgenome.core.task import TaskMode, TaskStore
    from agentgenome.space.git_ws import GitWorkspace

    try:
        task = TaskStore(root).get(task_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if task.mode is not TaskMode.INTERACTIVE:
        raise HTTPException(
            status_code=400,
            detail=f"{task_id} 不是交互式任务。"
            "在任务的工作区里改代码要用 `submit --interactive` 建的任务;"
            "只是想问问它为什么这么改的话,开一个只读会话关联它就行。",
        )
    worktree = GitWorkspace(root).worktree_path(task_id)
    if not worktree.is_dir():
        raise HTTPException(
            status_code=409,
            detail=f"{task_id} 的隔离工作区还没建好(当前在 {task.state.value})。先推进到开发态。",
        )
    return worktree


def _block(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": int(payload.get("seq", 0)),
        "kind": payload.get("kind", "text"),
        "text": payload.get("text", ""),
        "detail": payload.get("detail", {}),
    }


#: 列表里那一行预览的长度。**一行放不下就截断**——它是用来认出"是哪一场"的,不是摘要。
_PREVIEW_CHARS = 40


def _summary(root: Path, session: Session, service: SessionService) -> SessionSummary:
    """会话摘要 + 列表要用的派生字段。

    **在这一层拼,不进 `Session`。** 员工显示名住在花名册、最后一句住在日志面、是否
    在生成中住在服务的进程内注册表,都不是会话自己的状态;塞进领域对象等于让它去认识
    三个本来不该认识的东西,而这些字段只有展示要用。
    """
    summary = SessionSummary.of(session)
    summary.employee_name = _employee_name(root, session.employee_id)
    summary.last_question = _last_question(root, session.id)
    summary.generating = service.is_generating(session.id)
    return summary


def _employee_name(root: Path, employee_id: str) -> str:
    """员工给人看的名字。查不到就退回 id——列表里少一列比整页报错好。"""
    try:
        return load_employees(workspace_employees_root(root), strict=False).get(
            employee_id
        ).display_name
    except EmployeeNotFound:
        return employee_id


def _last_question(root: Path, session_id: str) -> str:
    """最后一条**用户**消息的一行预览。

    取用户那条而不是员工那条:标题重复时,用户想认出的是"我当时问的是什么",而员工的回答
    往往以同样的套话开头。
    """
    asked = [
        str(item.get("text") or "")
        for item in log.read(root, session_id)
        if (item.get("detail") or {}).get("role") == "user"
    ]
    if not asked:
        return ""
    text = asked[-1].strip().replace("\n", " ")
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"


def _get(root: Path, session_id: str) -> Session:
    try:
        return SessionStore(root).get(session_id)
    except SessionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _root(request: Request) -> Path:
    from agentgenome.server.app import _root as resolve

    return resolve(request)


def _service(request: Request, root: Path) -> SessionService:
    """取这个工作区的会话服务。

    **按工作区缓存。** 服务持有已开会话的运行时句柄,每次新建一个的话,第二条消息会因为
    "会话还没开起来"而失败——而症状是第一条能发、第二条不能。
    """
    cache: dict[str, SessionService] = request.app.state.session_services
    key = str(root)
    if key not in cache:
        cache[key] = SessionService(
            workspace_root=root,
            store=SessionStore(root),
            runtimes=_session_runtimes(request, root),
        )
    return cache[key]


def _session_runtimes(request: Request, root: Path) -> dict[str, object]:
    """这个工作区能开会话的运行时。

    **按工作区解析,不建一份全局的。** 工作区注册表可以挂多个工作区,各有自己的根配置;
    建一份全局的等于让第一个工作区的运行时配置管住所有工作区,而这种错误表现为"某个
    工作区的会话行为莫名其妙和别的工作区一样",极难归因。

    应用状态里那个字典是**覆盖项**而不是唯一来源:非空就用它(单元测试直接塞替身),空就
    按该工作区的配置真的装配一遍。早先它是唯一来源、且生产代码里没有任何地方往里写,于是
    控制面攥着一个永远为空的注册表——**而 e2e 因为直接往里塞替身,把装配整个跳过了**,
    这条路因此从没被执行过。见 PRD 29。
    """
    override: dict[str, object] = dict(request.app.state.session_runtimes)
    if override:
        return override
    try:
        return dict(build_session_runtimes(load_config(root)))
    except RuntimeAssemblyError as error:
        # 根配置搭不出注册表是运维要改的事,不是这次请求的错。**照原话传出去**——
        # 装配那一层已经说清了是哪个名字、已知的有哪些。
        raise HTTPException(status_code=409, detail=str(error)) from error


__all__ = ["mount_sessions"]
