"""知识初始化的阶段推进器:把扫描、闸门、深读、汇总串成一条真流水线。

五个阶段此前各自就绪、各有测试,但**没有任何东西把它们连起来**:`knowledge plan` 停在闸门,
`genome reinit` 只建任务,深读收一个 `run(module_id)` 回调,汇总与重建是纯函数。结果是一条
"每一节都能跑、整条跑不起来"的管道——而那种状态最容易被当成做完了,因为每一节的测试都是绿的。

这一层就是缺的那个驱动,与 `jobs.orchestrator` 之于研发任务同构:

    查处理器 → 跑这一阶段 → 拿到事件 → 过迁移表 → 落库 → 记事件

## 为什么不塞进研发任务那台编排器

两类任务的状态集没有交集,处理器表也就没有一行是共用的。合成一台的话,读代码的人要先在
每个分支里判断"这条对哪一类成立"——而这两台机器加起来最有价值的东西正是"迁移即文档"。

## 契约在汇总时才合并,不在每个模块落地时合并

`apply_staged` 对 `interfaces` 是**整段替换**。逐模块调用它的话,第二个模块的产出会把
第一个模块报上来的契约整段抹掉,而症状是"契约索引里少了一半"——没有报错,只是少了。所以深读
阶段只把每个模块**已验证的 staging 树**收在任务目录里,汇总阶段一次性 `merge_contracts`
再原子应用(PRD 34:搬运源从"解包 JSON"变成"拷贝已验证文件")。

## 逐模块的作业目前是串行的

派发看起来是并发的(`deep_read_modules` 用 `gather`),但 `AgentPool` 会按 **task_id** 上一把
任务锁,而同一次初始化的所有模块作业共用一个 task_id——于是它们排队跑。这不是疏忽:那把锁
同时护着越权检查的基线快照与回滚,而这些作业写的是同一个 Workspace 的 `genome/**`。放开它
之前得先让基线按作业分开,否则并发跑的两个作业会互相看到对方改到一半的现场。

**写在这里是因为它看起来像并发。** `genome_tasks.concurrent_jobs` 在这条路上目前不起作用,
不写的话下一个人会以为调大它就能加速。

## 知识变更要进版本面

汇总那一步写完知识树之后**提交进 git**,并在配了托管平台时把这次提交推成一条分支、开一个
知识 PR。不提交的话:知识变更不在版本面上(缺口检测因此看不见它)、`genome/project-map/
versions` 那份历史里没有它、而"这条认知从哪次经验来的"也就无从回答。

**先落地再评审,不是先评审再落地。** PRD 22 写的是"整体作为一次知识 PR 提交";这里的差别
是知识**同时**留在工作区里。理由:一棵没被合入就不存在的知识树,下一个任务用不上——而
ADR-0003 要求的"地图提前完备"正是为了让它立刻可用。PR 是评审记录与回滚入口,不是生效开关。
没配远端时只提交,不假装开过 PR。

## 一个模块失败不拖垮其余

四十九个模块里有一个超时,前面四十八个的产出不该跟着丢。深读收着异常记进清单(见
`deep_read`),汇总照跑——**部分成功是有意义的产出**,而失败的那几个在报告里点名,可以从
详情页就地重建。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from agentgenome import paths
from agentgenome.agents.pool import AgentPool
from agentgenome.agents.runtime import JobSpec
from agentgenome.approval.notify import Notification, send
from agentgenome.config import Config, load_config
from agentgenome.core.events import ORCHESTRATOR, EventLog, LogKind
from agentgenome.core.genome_driver import Applied, GenomeDriver
from agentgenome.core.genome_gate import read_answer, write_draft
from agentgenome.core.genome_task import (
    GenomeTask,
    GenomeTaskKind,
    GenomeTaskState,
    GenomeTaskStore,
    Origin,
)
from agentgenome.core.genome_transitions import GenomeEffect, GenomeEvent, GenomeFacts
from agentgenome.core.scope import ScopePolicy
from agentgenome.core.store import task_dir
from agentgenome.core.task_ids import lane_budget
from agentgenome.employees import load_employees, workspace_employees_root
from agentgenome.genome import knowledge as knowledge_mod
from agentgenome.genome import staging as staging_mod
from agentgenome.genome.boundary import (
    NotReadyForBoundaries,
    propose_boundaries,
    scan_for_boundaries,
)
from agentgenome.genome.deep_read import (
    DeepReadResult,
    ModuleOutcome,
    deep_read_modules,
    read_progress,
    write_progress,
)
from agentgenome.genome.errors import GenomeValidationError
from agentgenome.genome.loader import load_project_map, load_tree
from agentgenome.genome.models import Module
from agentgenome.genome.rebuild import RebuildPlan, plan_rebuild, render_rebuild_report
from agentgenome.genome.rules import load_rules
from agentgenome.genome.scan import HotPath, ScanResult
from agentgenome.genome.summary import build_report, merge_contracts, render_report
from agentgenome.genome.tree import module_dir, overview_path, write_tree
from agentgenome.space.forge import ForgeError
from agentgenome.space.forge import select as select_forge
from agentgenome.space.gitcmd import EMPLOYEE_IDENTITY, GitError, git, git_out, is_repo_root

#: 逐模块产出落在任务目录里的位置。**一个模块一个目录**(`deep-read/<模块id>/staging/`
#: + 小票):合成一份的话,一个模块的产出写坏会让整次深读的结果都读不回来,而深读恰恰是
#: 最贵的那一段。
DEEP_READ_DIR = "deep-read"

#: 收进任务目录的小票文件名。产物目录里它叫 result.json;收进来改名,免得与
#: "深读产出"这个目录里的树文件混在一起时被当成又一个 JSON 信封。
RECEIPT_FILE = "receipt.json"

#: 扫描结果。闸门草案由它推出来,人在闸门上要看的"划分依据"也来自它。
SCAN_FILE = "scan.json"

#: 给人看的那一页。**落盘而不是只打印**——初始化跑几个小时,没人会守着终端等这段输出。
REPORT_FILE = "init-report.md"

#: 重建多出来的那一页:产物与现有卡片的差异。
REBUILD_REPORT_FILE = "rebuild-report.md"


class GenomeOrchestrator:
    """推动基因组任务往前走的那台机器。"""

    def __init__(
        self,
        root: Path,
        pool: AgentPool | None = None,
        runtime_name: str | None = "claude-code",
        config: Config | None = None,
    ) -> None:
        self.root = Path(root)
        # 只推不派发时(扫描、汇总都不烧 token)不必逼调用方造一个池。
        self.pool = pool if pool is not None else AgentPool({})
        #: `None` 表示**用员工自己声明的运行时**——与研发任务的派发同一条纪律
        #: (`genome.dispatch`:缺省用员工声明的那个,显式传值是覆盖)。CLI 的
        #: `--runtime` 走覆盖;服务端的后台驱动走 None,于是测试切换运行时的方式
        #: 与研发任务一致:改员工定义,不塞替身。
        self.runtime_name = runtime_name
        self.config = config or load_config(self.root)
        self.store = GenomeTaskStore(self.root)
        self.log = EventLog(self.root)
        self.driver = GenomeDriver(self.store, self.log, enforce_budget=self.config.budgets.enforce)

    def _job_runtime(self, employee_id: str) -> str:
        """这个 Job 跑在哪个运行时上:显式覆盖优先,否则用员工声明的。"""
        if self.runtime_name is not None:
            return self.runtime_name
        registry = load_employees(workspace_employees_root(self.root), strict=False)
        return registry.get(employee_id).runtime

    # --- 推进 ----------------------------------------------------------------

    async def advance(self, task_id: str) -> GenomeTask:
        """把一个基因组任务推进一步。

        **等人的那一步原样返回。** 待确认不是终态,但它也不是"该被推一下"的状态——推它等于
        替人回答。终态同理。
        """
        task = self.store.get(task_id)
        handler = _HANDLERS.get(task.state)
        if task.is_terminal or handler is None:
            return task
        if task.kind is GenomeTaskKind.DISTILL:
            # 蒸馏由研发任务那台编排器内联驱动(见 `genome.evolution.record`)。两台机器
            # 同时推同一条记录的话,状态会在两边各推一半,而谁推的那一半查不出来。
            return task
        return await handler(self, task)

    async def drain(self) -> tuple[GenomeTask, ...]:
        """把每个未了结的基因组任务各推一步。

        **一轮只推一步。** 一口气推到底的话,一个卡在某阶段反复失败的任务会把这一轮的其余
        任务饿死;调用方多调几次就是了,而每一步都落了盘,中间断掉不丢进度。
        """
        return tuple([await self.advance(task.id) for task in self.store.unsettled_tasks()])

    # --- 各阶段 --------------------------------------------------------------

    async def _scan(self, task: GenomeTask) -> GenomeTask:
        """① 确定性扫描。

        **只有全量初始化会停在闸门。** 按模块重建的边界早就拍过板了,再问一次人只会让他
        点一次"就这样"——一个每次都被无脑确认的闸门,下一次真有问题时也会被无脑确认。
        """
        if task.kind is not GenomeTaskKind.INIT:
            return await self._read_modules(task, self._modules_of(task))

        try:
            scanned = scan_for_boundaries(self.root, self.config.genome_tasks.hot_path_since_days)
        except NotReadyForBoundaries as exc:
            return self._fail(task, str(exc))
        self._write(task, SCAN_FILE, json.dumps(scanned.as_dict(), ensure_ascii=False, indent=2))
        write_draft(self.root, task.id, propose_boundaries(scanned))
        applied = self.driver.deliver(task.id, GenomeEvent.DRAFT_READY)
        # 草案就绪要通知该确认的人:全异步的意义是人不必守着终端,而不守着就必须被叫。
        self._run_effects(applied)
        return applied.task

    async def _deep_read(self, task: GenomeTask) -> GenomeTask:
        """③ 逐模块深读。模块清单来自**人在闸门上给的那份最终列表**,不是草案。"""
        applied = self._apply_boundaries(task)
        if applied is not None:
            return applied
        return await self._read_modules(task, self._modules_of(task))

    def _apply_boundaries(self, task: GenomeTask) -> GenomeTask | None:
        """把人在闸门上确认的那份模块列表写进项目地图。顺利就返回 `None`。

        **不写的话,人做的合并、拆分、改名全都白做了。** 只拿它决定"读哪几个"的那一版里:
        剔除碰巧生效(它是现有 id 的子集),而合并出来的新模块会在汇总那一步撞上"结果里出现
        未知模块"——一次跑了几十分钟的初始化,死在人已经答对了的那个环节上。

        **只动模块清单,不碰认知字段。** 已有模块的语言、摘要、置信度都留着:人在闸门上答的
        是"有哪几个模块",不是"它们是什么样"。
        """
        answer = read_answer(self.root, task.id)
        wanted = [item for item in (answer or {}).get("modules") or [] if isinstance(item, dict)]
        if not wanted:
            return None
        try:
            project_map = load_project_map(self.root)
        except GenomeValidationError as error:
            return self._fail(task, f"项目地图读不出来,落不了闸门上的划分:{error.render()}")

        known = {module.id: module for module in project_map.modules}
        modules = []
        for row in wanted:
            module_id = str(row.get("id") or "")
            paths = [str(item) for item in row.get("paths") or [] if str(item).strip()]
            existing = known.get(module_id)
            if existing is not None:
                modules.append(existing)
                continue
            if len(paths) != 1:
                # 闸门的校验器已经挡住这一条(见 `genome_gate.module_boundary_answer`)。
                # 这里再挡一次是因为答复文件也可能被手工改过——而这一步之后就是写地图了。
                return self._fail(
                    task, f"闸门答复里的 {module_id} 覆盖了 {len(paths)} 个目录,一个模块只能一个"
                )
            modules.append(Module(id=module_id, path=paths[0]))
        project_map.modules = modules
        write_tree(self.root, project_map)
        return None

    async def _summarise(self, task: GenomeTask) -> GenomeTask:
        """④⑤ 汇总与写入:合并契约、原子应用 staging、产出给人看的那一页。"""
        collected = self._collect(task)
        if not collected:
            return self._fail(task, "没有任何模块产出可汇总——深读全军覆没")

        # **树上已有的契约也算一份上报。** 不算的话,一次只读了一个模块的重建会把其余模块
        # 报过的契约整段抹掉——没有报错,只是少了,而下游"这次改动会波及谁"正是按契约找关联方。
        # 这与"逐模块写入会互相覆盖"是同一个坑,只是从重建这条路进来。
        reported = [
            (list(fragment.interfaces or ()), list(fragment.datastores or ()))
            for fragment in collected.values()
        ]
        try:
            known = load_project_map(self.root)
            reported.insert(0, (list(known.interfaces), list(known.datastores)))
        except GenomeValidationError:
            # 树读不出来时不硬合:那时 `apply_staged` 自己会拦下来,理由比这里更准。
            pass
        interfaces, datastores = merge_contracts(reported)
        staged = staging_mod.StagedTree(
            modules=tuple(module for fragment in collected.values() for module in fragment.modules),
            interfaces=tuple(interfaces),
            datastores=tuple(datastores),
        )
        # **差异要在写之前算。** 写完再比的话,`before` 已经是新内容,那份 diff 永远是空的
        # ——而它存在的理由正是让人看见"这次刷新会覆盖掉什么"。
        plan = self._plan_rebuild(task, staged)
        try:
            update = staging_mod.apply_staged(self.root, staged, self.config.knowledge)
        except GenomeValidationError as error:
            # **停在汇总中,不判死。** 深读是整条流水线里唯一贵的一段,它的产出还在任务目录
            # 里;判成终态失败的话那批产出再也用不上了——人修完树想重跑④都没有入口。留在
            # 原地、把原因记进事件流,下一次 `knowledge run` 直接从这里接着来。
            self._note(task, f"知识写入被校验拦下,停在汇总中:{error.render()}")
            return task

        progress = read_progress(self.root, task.id) or DeepReadResult()
        report = build_report(
            load_tree(self.root),
            progress,
            human_adjustments=read_answer(self.root, task.id) or {},
            # 小票进校对清单,**不进裁决**:questions 是员工唯一能说"这里我拿不准"的地方,
            # 契约改造不该把人工复核的这个入口弄丢(PRD 34 US4)。
            receipts=self._receipts(task),
        )
        self._write(task, REPORT_FILE, render_report(report))
        if plan is not None:
            self._write(task, REBUILD_REPORT_FILE, render_rebuild_report(plan))
        self._publish(task, update.files_written)
        return self.driver.deliver(task.id, GenomeEvent.SUBMITTED).task

    def _publish(self, task: GenomeTask, files: list[str]) -> None:
        """把这次知识变更提交进版本面,并在配了托管平台时开一个知识 PR。

        **只提交这次写过的那几个文件。** 拿 `genome/**` 整个 add 的话,同一个检出里别人未
        提交的改动(人手改的规则、蒸馏刚写的经验卡片)会被一起打包进来——署名成员工、标题
        写着知识更新,而那不是它。

        **提交失败不判死这次汇总。** 知识已经写进工作区了,任务确实完成了它该做的事;
        提交是把它送上版本面的那一步,失败要留痕、要能重来,但把一次成功的初始化改判成失败
        会让人以为知识没建成——而它建成了。
        """
        if not files:
            # 一个字都没改就没有可提交的。**不造空提交**——版本面上一条什么都没动的记录,
            # 会让"认知是什么时候变的"多出一个假答案。
            return
        try:
            if not is_repo_root(self.root):
                return
            git(self.root, "add", "--", *files)
            git(
                self.root,
                *EMPLOYEE_IDENTITY,
                "commit",
                "-m",
                f"knowledge({task.kind.value}): {task.subject or '全量'}\n\nTask: {task.id}\n",
                "--",
                *files,
            )
            rev = git_out(self.root, "rev-parse", "HEAD")
        except GitError as error:
            self._note(task, f"知识变更没能提交进版本面(知识已写入工作区): {error}")
            return
        self._open_pr(task, rev)

    def _open_pr(self, task: GenomeTask, rev: str) -> None:
        """把这次提交推成一条分支并开 PR。**没配远端就只提交,不假装开过。**

        托管平台那一侧的失败一律收着:`gh` 没装、远端不通、分支已存在——这些都不该让一次
        已经写好并提交了的知识更新变成失败。**收着但要留痕**,否则"我的知识 PR 呢"查无可查。
        """
        try:
            remote = git_out(self.root, "remote", "get-url", "origin")
        except GitError:
            return
        branch = f"knowledge/{task.id}"
        try:
            git(self.root, "push", "--quiet", "origin", f"{rev}:refs/heads/{branch}")
            pr = select_forge(self.config.platform.git_host).open_pr(
                remote,
                head=branch,
                base=self.config.platform.protected_branch,
                title=f"知识更新:{task.subject or '全量初始化'}",
                body=(
                    f"来源任务:{task.id}\n\n"
                    "这次更新已经写进工作区的知识树——**PR 是评审记录与回滚入口,不是生效开关**。"
                    "一棵没被合入就不存在的知识树,下一个任务用不上。\n"
                ),
            )
        except (GitError, ForgeError, OSError) as error:
            # `OSError` 也收:默认的 `gh` 客户端在没装 gh 的机器上抛 `FileNotFoundError`,
            # 而那与"这次知识更新成不成功"毫无关系。
            self._note(task, f"知识 PR 没开成(变更已提交): {error}")
            return
        # 只记指针:改成了什么去那个 PR 里看,事件面不存内容。
        self.log.append(
            task.id,
            actor=ORCHESTRATOR,
            kind=LogKind.GENOME_PR,
            payload={
                "asset": "knowledge",
                "pr": pr.as_dict(),
                "source_task_id": task.source_task_id or "",
            },
        )

    def _plan_rebuild(self, task: GenomeTask, staged: staging_mod.StagedTree) -> RebuildPlan | None:
        """重建比初始化多的那一步:产物与现有卡片先做 diff。

        **只有重建做。** 全量初始化面对的是一棵空树,diff 出来就是"每一张都是新的"——那份
        清单没有信息量,而它会把真正要看的那份(重建覆盖了什么)淹掉。

        保护本身不靠这一步:写入路径认人工编辑标记。这一步产出的是**给人在 PR 上
        逐条比对的那一页**——"我改过的东西下次扫描就没了"会让人彻底放弃维护知识,而让他看见
        哪些被动了、哪些没被动,是唯一能挡住这件事的东西。
        """
        if task.kind is not GenomeTaskKind.REINIT or not task.subject:
            return None
        produced: dict[str, str] = {}
        for entry in staged.modules:
            if entry.doc_text is not None:
                produced[overview_path(entry.id)] = entry.doc_text
            for feature in entry.features or ():
                if feature.card_text is not None:
                    relative = module_dir(self.root, entry.id) / f"features/{feature.id}.md"
                    produced[str(relative.relative_to(self.root))] = feature.card_text
        # **这一轮没再产出的卡片也要进 diff。** 它们会被退役(见 `staging._retire_features`)
        # ——而"这次刷新删掉了哪张卡片"恰恰是人最需要看见的那一条,不列出来的话它是静默的。
        for card in sorted((module_dir(self.root, task.subject) / "features").glob("*.md")):
            produced.setdefault(str(card.relative_to(self.root)), "")
        return plan_rebuild(self.root, task.subject, produced)

    # --- 深读的执行 ----------------------------------------------------------

    async def _read_modules(self, task: GenomeTask, module_ids: list[str]) -> GenomeTask:
        if not module_ids:
            return self._fail(task, "没有要读的模块——闸门答复里一个都没留下")

        # **每次派发前把预算同步进池。** 池是进程内的,重启之后它对"已经烧了多少"一无所知
        # ——不同步的话,重启就等于把这个任务的预算清零,而那是硬上限那条承诺的反面。
        self.pool.set_task_budget(
            task.id,
            (
                task.budget_tokens
                or lane_budget(
                    self.config.budgets.per_task_tokens,
                    self.config.genome_tasks.per_task_tokens,
                    task.id,
                )
                if self.config.budgets.enforce
                else None
            ),
            task.tokens_used,
        )
        # **已经有产出的模块不重派。** 深读跑到一半崩了再起来时,重派一遍等于把最贵的那段
        # 重跑一次;而这与研发那台编排器"先看产物在不在"是同一条。
        done = {item.name for item in self._produced(task)}
        pending = [item for item in module_ids if item not in done]
        if not pending:
            return self.driver.deliver(task.id, GenomeEvent.READ_DONE).task

        scanned = self._scan_result(task)
        result = await deep_read_modules(
            pending,
            run=lambda module_id: self._read_one(task, module_id),
            hot_paths=[item.path for item in scanned.hot_paths],
            # **每读完一个就落盘。** 只在结束时写的话,进度在唯一有人盯着的那段时间里是空的。
            on_update=lambda found: self._record(task, found, already=sorted(done)),
        )
        # 池是进程内的,而"这个任务烧了多少"要活过重启——不落库的话预算那条硬上限只在
        # 单次进程内成立,而界面上这个任务的成本永远是 0。
        self._charge(task)
        if not result.ok and not done:
            return self._fail(task, f"{len(result.failed)} 个模块全部深读失败")
        return self.driver.deliver(task.id, GenomeEvent.READ_DONE).task

    async def _read_one(self, task: GenomeTask, module_id: str) -> ModuleOutcome:
        """派一个作业去读一个模块。

        `subject` 带上模块 id:同 employee 同 procedure 的作业会同时派出几十个,不带的话回放键
        全撞在一起——回放会给每个模块返回同一份产出,而测试照样是绿的。

        **Job 的成败由 staging 校验裁决**(`output_check`,PRD 34):员工把树片段写成
        产物目录 `staging/` 下的真实文件,校验不过按契约失败走重试——拒绝原因是逐文件的
        问题清单,而 staging 在重试间保留,第二次尝试只需修坏的文件。
        """
        try:
            project_map = load_project_map(self.root)
            rules = load_rules(self.root)
        except GenomeValidationError as error:
            return ModuleOutcome(module_id, ok=False, detail=error.render())

        directory = task_dir(self.root, task.id)
        context_file = directory / "context" / f"{module_id}.md"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        output_dir = directory / "artifacts" / module_id
        context_file.write_text(
            knowledge_mod.build_prompt(
                project_map, self.root, focus=module_id, output_dir=output_dir
            ),
            encoding="utf-8",
        )

        spec = JobSpec(
            task_id=task.id,
            employee_id=knowledge_mod.EMPLOYEE_ID,
            procedure_id=knowledge_mod.PROCEDURE_ID,
            procedure_version=knowledge_mod.PROCEDURE_VERSION,
            round=1,
            subject=module_id,
            workdir=self.root,
            context_file=context_file,
            output_dir=output_dir,
            output_schema=knowledge_mod.RECEIPT_SCHEMA,
            output_check=staging_mod.knowledge_output_check(
                self.root, focus=module_id, limits=self.config.knowledge
            ),
            timeout_s=self.config.limits.job_timeout_s,
            max_tokens=self.config.budgets.per_job_tokens,
            enforce_token_limit=self.config.budgets.enforce,
            tools_allow=["Read", "Grep", "Glob", "Bash", "Write"],
            tools_deny=["WebFetch", "WebSearch"],
            # 架构员工只补认知。带 scope 让它走**与其他 Job 同一道**越权检查:那道检查在
            # 池里无条件执行,越权即回滚并留下结构化报告。
            scope=ScopePolicy(write_paths=(f"{paths.GENOME}/**",)).with_protected(
                rules.protected.paths_for(knowledge_mod.EMPLOYEE_ID)
            ),
        )
        found = await self.pool.submit(spec, runtime_name=self._job_runtime(spec.employee_id))
        if not found.ok:
            return ModuleOutcome(
                module_id, ok=False, detail=f"{found.failure_kind.value}: {found.failure_detail}"
            )
        # 产出**先收着不写入**,理由见模块说明(契约要到汇总才合并)。收的是**已验证的
        # staging 树 + 小票**,不是 JSON 信封。
        collected = task_dir(self.root, task.id) / DEEP_READ_DIR / module_id
        if collected.is_dir():
            shutil.rmtree(collected)
        shutil.copytree(output_dir / staging_mod.STAGING_DIR, collected / staging_mod.STAGING_DIR)
        receipt = output_dir / "result.json"
        if receipt.is_file():
            shutil.copy2(receipt, collected / RECEIPT_FILE)
        return ModuleOutcome(module_id, ok=True)

    # --- 内部 ----------------------------------------------------------------

    def _modules_of(self, task: GenomeTask) -> list[str]:
        """这次要读哪几个模块。

        按模块重建只读它自己那一个;全量初始化读**人在闸门上确认的那份列表**——用草案的话,
        人做的那次合并、拆分、剔除全都白做了,而界面上他看到的是"已确认"。
        """
        if task.subject:
            return [task.subject]
        answer = read_answer(self.root, task.id)
        modules = (answer or {}).get("modules") or []
        return [
            str(item.get("id")) for item in modules if isinstance(item, dict) and item.get("id")
        ]

    def _scan_result(self, task: GenomeTask) -> ScanResult:
        """读回扫描结果。读不到就当没有热区——它只影响派发顺序,不该让整段深读跑不起来。"""
        target = task_dir(self.root, task.id) / SCAN_FILE
        if not target.is_file():
            return ScanResult()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ScanResult()
        return ScanResult(
            hot_paths=tuple(
                HotPath(str(item.get("path", "")), int(item.get("changes", 0)))
                for item in payload.get("hot_paths") or []
            )
        )

    def _collect(self, task: GenomeTask) -> dict[str, staging_mod.StagedTree]:
        """把逐模块的 staging 片段装载回来。**装不出来的那一份跳过并留痕,不炸整次汇总。**

        片段在收进来之前已经过校验,这里装载失败只剩一种来路:任务目录被手改过。
        跳过是老口径("读不出来的那一份跳过"),但要留痕——静默少一个模块与"它本来就没跑"
        在报告上分不开。
        """
        found: dict[str, staging_mod.StagedTree] = {}
        for item in self._produced(task):
            try:
                found[item.name] = staging_mod.load_knowledge_staging(
                    self.root, item / staging_mod.STAGING_DIR, limits=self.config.knowledge
                )
            except GenomeValidationError as error:
                self._note(task, f"{item.name} 的深读产出装载失败,这次汇总跳过它:{error.render()}")
        return found

    def _receipts(self, task: GenomeTask) -> dict[str, dict[str, Any]]:
        """逐模块的小票。读不出来给空——小票只补充校对清单,不参与裁决。"""
        return {
            item.name: staging_mod.read_receipt(item / RECEIPT_FILE)
            for item in self._produced(task)
            if (item / RECEIPT_FILE).is_file()
        }

    def _record(self, task: GenomeTask, found: DeepReadResult, already: list[str]) -> None:
        """把进度落盘。**上一轮已经读完的那些要带上**——不带的话,一次续跑会把进度文件
        重写成"只读了这一轮的几个",而人看到的是进度倒退。"""
        write_progress(
            self.root,
            task.id,
            replace(
                found,
                done=[*already, *found.done],
                planned=[*already, *found.planned],
            ),
        )

    def _charge(self, task: GenomeTask) -> None:
        """把这一轮烧掉的 token 记进任务。读不到用量就不动——**填 0 会让成本看板悄悄少算**。"""
        spent = self.pool.tokens_used(task.id)
        if spent > task.tokens_used:
            self.store.save(self.store.get(task.id).evolve(tokens_used=spent))

    def _produced(self, task: GenomeTask) -> list[Path]:
        """已有产出的模块目录:`deep-read/<模块id>/`,里面是已验证的 staging 树 + 小票。"""
        directory = task_dir(self.root, task.id) / DEEP_READ_DIR
        if not directory.is_dir():
            return []
        return sorted(
            item
            for item in directory.iterdir()
            if item.is_dir() and (item / staging_mod.STAGING_DIR).is_dir()
        )

    def _note(self, task: GenomeTask, text: str) -> None:
        self.log.append(task.id, actor=ORCHESTRATOR, kind=LogKind.NOTE, payload={"note": text})

    def _write(self, task: GenomeTask, relative: str, text: str) -> Path:
        target = task_dir(self.root, task.id) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def _fail(self, task: GenomeTask, reason: str) -> GenomeTask:
        """停下来,理由用「下一步该从哪查」的口径写,并把该通知的人通知到。"""
        applied = self.driver.deliver(task.id, GenomeEvent.FAILED, GenomeFacts(stop_reason=reason))
        self._run_effects(applied)
        return applied.task

    def _run_effects(self, applied: Applied) -> None:
        """执行迁移要求的副作用。

        **迁移表说"该做什么",执行在这里。** 不执行的话,那张表上的 `NOTIFY_OWNER` 就是一句
        没有效果的声明——而它存在的全部理由是"人敲了命令正在等结果的初始化失败了要通知他"。
        一个静默失败的初始化,人要等到下次想起来看看板才发现。

        **通知失败不影响任务。** 通知渠道是可选的;没配就是这个部署不需要,而发不出去也不该
        让一次已经停下来的迁移再抛一次。
        """
        task = applied.task
        for effect in self.driver.pending_effects(applied):
            if effect is GenomeEffect.NOTIFY_OWNER and task.origin is not Origin.HUMAN:
                # 系统自发的失败只留一条事件。**同样是失败,响应完全不同**——判据是
                # "有没有人在等",不是"失不失败"。
                continue
            if effect is GenomeEffect.ARCHIVE_REPORT:
                continue
            ok, detail = send(
                self.config.approval.notify.webhook,
                Notification(task_id=task.id, title=task.title, risk=effect.value),
            )
            if not ok:
                self._note(task, f"通知没发出去({effect.value}): {detail}")


_Handler = Callable[[GenomeOrchestrator, GenomeTask], Awaitable[GenomeTask]]

#: 状态 → 处理器。**一张表,不是一串 if**:加一个阶段时看得见自己要补哪一行,而
#: `AWAITING_CONFIRMATION` 不在表里这件事本身就是文档——那一步在等人,不该被推。
_HANDLERS: dict[GenomeTaskState, _Handler] = {
    GenomeTaskState.SCANNING: GenomeOrchestrator._scan,
    GenomeTaskState.DEEP_READ: GenomeOrchestrator._deep_read,
    GenomeTaskState.SUMMARISING: GenomeOrchestrator._summarise,
}


__all__ = [
    "DEEP_READ_DIR",
    "RECEIPT_FILE",
    "REBUILD_REPORT_FILE",
    "REPORT_FILE",
    "SCAN_FILE",
    "GenomeOrchestrator",
]
