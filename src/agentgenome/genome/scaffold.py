"""Workspace 骨架生成(`workspace-init`)。

这一步完全确定性,不经过任何 Agent。它的产出必须立刻能通过基因组校验——
Workspace 从第一秒起就是合法状态,不必等 knowledge-init 参与。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from agentgenome import paths
from agentgenome.config import render_default_config
from agentgenome.core.events import ORCHESTRATOR, SYSTEM_SUBJECT, EventLog, LogKind
from agentgenome.genome import craft
from agentgenome.genome.models import Module, ProjectInfo, ProjectMap
from agentgenome.genome.roster import scaffold_roster
from agentgenome.genome.tree import write_tree
from agentgenome.space.gitcmd import (
    ORCHESTRATOR_IDENTITY,
    GitError,
    git,
    git_out,
    is_local_url,
)
from agentgenome.verification import (
    NeedsConfirmation,
    Ready,
    resolve_verification,
    write_pending_verification,
    write_verification_spec,
)

#: 归档根改了位置的话这里必须跟着改——否则审计包会进 git,把协作仓撑爆。
#: 把 `evidence.archive_dir` 指到 Workspace 内的别处时,那条路径要自己补进忽略规则。
GITIGNORE = f"""\
# 运行态高频写入,入库会污染历史;任务结束后只归档摘要到基因组。
{paths.TASKS}/
# 审计包:打包的快照,不是会演进的资产。入库会把协作仓撑爆。
{paths.ARCHIVE}/
# 手艺的物化目录:genome/procedures/ 才是唯一事实源,这里是每 Job 重建的派生视图。
# 入库的话员工对挂载副本的改动就持久化了,而"篡改不留痕"正是靠它不入库来保证的。
{craft.MOUNT_SUBPATH}/
__pycache__/
*.pyc
"""

ARCHITECTURE_TEMPLATE = """\
# 架构规则

本文档面向人书写,其中的 `rules` 代码块由编排器与门禁消费。
块外的正文纯粹给人看,想写多少写多少。

```rules
forbidden_deps: []
layering: []
```

## 依赖方向

（待补充：哪些模块不得反向依赖哪些模块，以及为什么。）
"""

CODING_TEMPLATE = """\
# 编码规范

（待补充：命名、错误处理、日志、测试组织方式。数字员工会读这份文档。）
"""

PROTECTED_TEMPLATE = """\
# 受保护路径与高风险路径。
# protected_paths: 触碰即判越权,Job 直接失败并回滚工作区。
#   写成 `- <glob>` 表示谁都不能动;要留口子就写 writable_by,值是员工 id。
#   豁免只能写在这里,不能写在员工定义里——员工要是能给自己开豁免,这份名单就只是建议。
# high_risk:      命中即强制转人工审批。
protected_paths:
  # 规则是项目的宪法,只有架构员工能改。开发员工能改规则就等于能自己给自己开绿灯。
  - path: genome/rules/**
    writable_by: [arch-employee]
  - .github/**
  - .gitmodules
high_risk:
  - id: migrations
    description: 数据库迁移不可逆
    path_globs: ["**/migrations/**"]
  - id: mass-deletion
    description: 单次改动删除量过大
    deleted_lines_gt: 500
"""

IMPACT_TEMPLATE = """\
# 变更影响规则:什么样的 diff 必须触发集成测试。
# 规则优先于 Agent 判断——确定的事由规则保证稳定,灰色地带才交给 Agent 补位。
rules:
  - id: interface-schema
    description: 触碰跨模块契约
    match: {touches_interface_schema: true}
    requires_itest: true
  - id: migrations
    description: 触碰数据库迁移
    match: {touches_migrations: true}
    requires_itest: true
  - id: cross-module
    description: 一次改动跨越两个以上模块
    match: {crosses_modules_gte: 2}
    requires_itest: true
  - id: deploy-files
    description: 触碰部署或集成测试编排文件
    match: {path_globs: ["itest/**", "deploy/**", "docker-compose*.y*ml", "**/Dockerfile"]}
    requires_itest: true
"""

_SKELETON_DIRS = (
    paths.MODULES,
    paths.DECISIONS,
    paths.LESSONS,
    paths.RULES,
    paths.PROCEDURES,
    paths.EMPLOYEE_PROMPTS,
    paths.SCRIPTS,
    paths.TASKS,
)


class WorkspaceExistsError(RuntimeError):
    """目标目录已经是一个 Workspace。"""


class WorkspaceRemoteFailed(RuntimeError):
    """顶层 Workspace 远端无法配置或推送。"""


class MountFailed(RuntimeError):
    """某个业务仓挂不上。

    **git 的原文不够用。** 它不说是哪个仓(挂五个时人要逐个试),而"You are on a branch yet
    to be born"这种话既不指向根因也不指向动作——它的意思是"这个仓一个提交都没有",
    而那恰恰是绿地开发最自然的起点:刚在托管平台点了新建。

    认不出的错误**原样带上 git 的 stderr**:翻译不全时保留原文,比吞掉强。
    """

    def __init__(self, repo: RepoSpec, error: GitError) -> None:
        self.repo = repo
        self.error = error
        super().__init__(f"{repo.module_id}({repo.url}) 挂不上:{self._explain(error.stderr)}")

    def _explain(self, stderr: str) -> str:
        if "yet to be born" in stderr or "cloned an empty repository" in stderr:
            return (
                "这个仓一个提交都没有。先给它一个初始提交(比如一个 README)再挂——"
                "Workspace 本身不含业务代码,所以从零开始也需要一个能挂的仓。"
                "完整走法见设计文档的「从零开始:绿地项目怎么起步」。"
            )
        # **地址先判。** 反过来的话,一个名字里带着分支名的仓(`main-service`)地址不通时会被
        # 报成"没有这个分支",把人引向完全错误的排查方向——而这正是这段翻译存在的理由。
        if "does not exist" in stderr or "not found" in stderr or "Could not read from" in stderr:
            return f"这个地址读不到,确认它存在且你有权限。\n  {stderr.strip()}"
        # 判据是 git 自己说的那句话,不是"分支名出现在 stderr 里"——后者会被仓库名撞上。
        if self.repo.branch and ("is not a commit" in stderr or "Remote branch" in stderr):
            return f"这个仓上没有分支 {self.repo.branch}——要查的是分支名,不是地址。"
        return stderr.strip()


@dataclass(frozen=True)
class RepoSpec:
    """一个待挂载的业务仓。"""

    url: str
    module_id: str
    path: str
    branch: str | None = None

    @property
    def mount_point(self) -> str:
        return self.path.rstrip("/")


def derive_module_id(url: str) -> str:
    """从仓库地址推出模块 id:取末段、去掉 .git 后缀。"""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"\.git$", "", name)
    return name or "module"


#: 净化后什么都不剩时用它。给一个固定名字而不是报错:一个仓库名没法产出 slug 不该让
#: 整次初始化失败,而冲突后缀会保证第二个这样的仓也有地方去。
_FALLBACK_SLUG = "module"

#: Windows 上**任何扩展名下都创建不了**的名字。撞上的表现是 clone 在 Windows 机器上
#: 直接失败,而错误信息完全不指向"你的仓库叫 nul"这个根因。
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in range(1, 10)}
    | {f"lpt{digit}" for digit in range(1, 10)}
)

#: 不能直接出现在挂载点里的字符,连续的算一段。**用"安全字符的补集"而不是列举危险字符**:
#: 黑名单漏一个字符的后果是产出一个在某个平台上创建不了的目录,而那要等到别人 clone 时才发现。
#:
#: `_` 留在安全集里:它与 `-` 在任何文件系统上都是两个能共存的目录名,折掉它一分安全都不买,
#: 只损失对仓库名的忠实度。
_UNSAFE_RUN = re.compile(r"[^a-z0-9._-]+")


def slugify_mount(module_id: str) -> str:
    """把模块 id 净化成一个可以安全当目录名用的 slug。

    **净化的对象是名字,不是地址**——所以入参是已经推导好的 module id,不是 URL。

    `code-N` 那套编号免费带着"任何仓库名都产不出非法路径"这条性质。换成语义化路径之后
    它得自己挣回来,而挣的方式是把仓库名当**不可信输入**:

    - 转小写。目录名在 macOS 与 Windows 上不区分大小写,留着大写等于留一颗"在我机器上
      是两个目录、在 CI 上是一个"的雷;
    - 非白名单字符**连续折成一个** `-`,不是每字符一个——`a//b` 该是 `a-b` 而不是 `a--b`;
      非 ASCII 一并折掉,因为 macOS 用 NFD、Linux 用 NFC,同一个名字在两处是不同的字节
      序列,而挂载点要进 `.gitmodules` 并被逐字比对;
    - 去掉首尾的 `-` 与 `.`。`.foo` 是隐藏目录(会被一堆遍历逻辑跳过),`foo.` 在 Windows
      上创建不了;
    - 规避平台保留名;
    - 净化后为空则退化成一个固定名字(`.` 与 `..` 也走这条:它们被 strip 削成空串)。

    同名冲突不在这里处理:净化是无状态的纯函数,冲突是**计划级**的状态。见 `plan_repos`。
    """
    # `.` 与 `..` 不必单独判:`strip("-.")` 已经把它们削成空串,而空串走下面这条。
    # 单独列一次的话那半个条件永远走不到,读的人会以为它在防什么。
    slug = _UNSAFE_RUN.sub("-", module_id.lower()).strip("-.")
    if not slug:
        return _FALLBACK_SLUG
    # 保留名比对只看首段:Windows 的限制对 `con.txt` 同样成立。
    if slug.split(".", 1)[0] in _RESERVED:
        return f"{slug}-repo"
    return slug


def parse_repo_arg(value: str) -> tuple[str, str | None]:
    """解析 `--repo` 的取值:`<url>` 或 `<url>@<branch>`。

    只把**最后一个** `@` 之后的部分当分支,且要求它不含 `/` 与 `:`——否则
    `git@host:org/repo.git` 这种 SSH 地址会被误切。
    """
    url, sep, tail = value.rpartition("@")
    if sep and url and "/" not in tail and ":" not in tail and tail:
        return url, tail
    return value, None


def plan_repos(repo_args: list[str]) -> list[RepoSpec]:
    """把用户给的仓库地址排成挂载计划。

    **这是全系统唯一产出挂载路径的地方**,而它只在 init 时跑一次。"上游改名不影响挂载点"
    这条性质就是这么来的:路径产出一次、写进 `.gitmodules` 与根索引,此后一切都从那两处读,
    没有任何环节会拿远端地址重推一遍。

    挂载点是 `repos/<slug>/`,slug 由仓库名净化而来;模块 id 也取仓库名但**不做净化**。
    两个字段、两套规则、两个用途:id 是身份(跨文件引用、门禁索引、依赖声明),path 是位置。
    绝大多数情况下两者取值相同,那是巧合不是约束。
    """
    specs = []
    taken: set[str] = set()
    for raw in repo_args:
        url, branch = parse_repo_arg(raw)
        module_id = derive_module_id(url)
        slug = _unique_slug(slugify_mount(module_id), taken)
        taken.add(slug)
        specs.append(
            RepoSpec(
                url=url,
                module_id=module_id,
                path=f"{paths.REPOS.as_posix()}/{slug}/",
                branch=branch,
            )
        )
    return specs


def _unique_slug(slug: str, taken: set[str]) -> str:
    """同名仓库的第二个往后依次拿 `-2`、`-3`。

    **判的是净化后的 slug,不是原始仓库名。** `Order-Service` 与 `order_service` 净化之后
    是同一个目录;只比原始名的话这两个仓会被判成不冲突,然后挂到同一个路径上——第二次
    `git submodule add` 失败,或者更糟,悄悄覆盖第一个。
    """
    if slug not in taken:
        return slug
    suffix = 2
    while f"{slug}-{suffix}" in taken:
        suffix += 1
    return f"{slug}-{suffix}"


def write_project_map_skeleton(root: Path, project_name: str, repos: list[RepoSpec]) -> None:
    """从挂载计划确定性地生成知识树骨架。

    只填得出来的东西:id 与 path。summary / depends_on / 契约这些需要深读代码才知道的,
    留空待 knowledge-init 补齐；验证命令由独立的确定性发现器写入版本化规格。
    **功能点一个不建**——那一层知识此刻还不存在,凭空捏造就是污染。
    """
    skeleton = ProjectMap(
        project=ProjectInfo(name=project_name, summary=""),
        modules=[Module(id=repo.module_id, path=repo.path) for repo in repos],
    )
    write_tree(root, skeleton)


def init_workspace(
    root: Path,
    project_name: str,
    repos: list[RepoSpec],
    default_branch: str = "main",
    mount_repos: bool = True,
    workspace_remote: str | None = None,
) -> Path:
    """建出一个可用的 Workspace 并提交骨架。

    **要么全成,要么不留痕。** 骨架先写、业务仓后挂,任何一个仓挂不上,磁盘上都已经留下半个
    Workspace——而重跑会被上面那道"已存在"挡住。命令失败了,最自然的补救却被拒绝,用户唯一
    的出路是自己 `rm -rf`,且没有任何东西告诉他这件事。

    **只清自己建的。** 目标目录本来就在的话一律不清:一个失败的初始化不该有权删掉别人的东西。
    """
    if (root / ".git").exists() or (root / paths.ROOT_CONFIG).exists():
        raise WorkspaceExistsError(f"目标位置已存在 Workspace: {root}")

    # 在动手之前记下来。事后靠"目录里有没有东西"反推是不行的——那时候里面装的正是我们自己
    # 写进去的骨架。
    #
    # **空目录也算我的。** 只看"存在不存在"的话,`mkdir ws && agctl init ws` 这条再正常不过的
    # 用法会走进最坏的组合:目录本来就在,于是失败之后不清,而里面留着的半个骨架又让重跑撞上
    # "已存在"。人明明什么都没放进去,却被判定成"别人的东西"。
    existed = root.exists()
    mine = not existed or not any(root.iterdir())
    try:
        return _build(
            root,
            project_name,
            repos,
            default_branch,
            mount_repos=mount_repos,
            workspace_remote=workspace_remote,
        )
    except BaseException:
        if mine:
            shutil.rmtree(root, ignore_errors=True)
            if existed:
                # 它本来就在,只是空的。承诺是"回到执行之前的样子",而不是"连目录一起带走"。
                root.mkdir(parents=True, exist_ok=True)
        raise


def _build(
    root: Path,
    project_name: str,
    repos: list[RepoSpec],
    default_branch: str,
    mount_repos: bool = True,
    workspace_remote: str | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", f"--initial-branch={default_branch}")

    for relative in _SKELETON_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)
        # git 不记录空目录。不放占位文件的话,任何 clone 或 worktree 拿到的骨架
        # 都是残缺的——员工上岗时会发现该往哪写的目录不存在。
        (root / relative / ".gitkeep").write_text("")

    (root / ".gitignore").write_text(GITIGNORE)
    (root / paths.ROOT_CONFIG).write_text(render_default_config())
    (root / paths.ARCHITECTURE_RULES).write_text(ARCHITECTURE_TEMPLATE)
    (root / paths.CODING_RULES).write_text(CODING_TEMPLATE)
    (root / paths.PROTECTED_RULES).write_text(PROTECTED_TEMPLATE)
    (root / paths.IMPACT_RULES).write_text(IMPACT_TEMPLATE)

    # `mount_repos=False` 是界面建项目那条路:骨架同步就位(项目立刻可见),clone 作为
    # 基因组任务异步跑——三个仓可能要几分钟,塞进一个同步请求会在超时后留下半个 Workspace。
    # 知识树骨架照写:模块的 id 与 path 在挂载计划里就定了,挂没挂上不改变它们。
    if mount_repos:
        for repo in repos:
            _mount(root, repo)

    write_project_map_skeleton(root, project_name, repos)
    if mount_repos:
        for repo in repos:
            resolution = resolve_verification(repo.module_id, root / repo.path)
            if isinstance(resolution, Ready):
                write_verification_spec(root, resolution.spec)
            elif isinstance(resolution, NeedsConfirmation):
                write_pending_verification(root, repo.module_id, resolution)
    # 默认员工队伍与 code-develop。写进 Workspace 而不是藏在包里:调整角色定位
    # 应该等同于改一个文件、走一次 git 评审。
    scaffold_roster(root)

    git(root, "add", "-A")
    _commit(root, f"chore: 初始化 AgentGenome Workspace({project_name})")
    _record_initial_config(root)
    if not mount_repos:
        # 初始化意图必须先于任何远端副作用落盘。否则骨架 push 成功、mount plan 尚未写入
        # 时崩溃,重启认领会把一个没有业务仓的项目误判为已经就绪。
        write_mount_plan(root, repos)
    if workspace_remote:
        configure_workspace_remote(root, workspace_remote, default_branch)
    return root


def _record_initial_config(root: Path) -> None:
    """给建仓时带进来的那份配置补一条配置变更事件。

    **不是形式主义。** 缺口检测(`security.gaps`)比对的是"改过配置的提交"与"配置变更
    事件";不补这一条的话,每一个 Workspace 从建成的第一天起就永久带着一条缺口,而那条
    缺口的"嫌疑人"是系统自己。一份从第一行起就有已知噪音的报告,没有人会看第二遍。

    补记录而不是在检测那边开一个例外:例外清单一旦开头,下一个人就会问"那这条呢"——而那
    正是这类检测失效最常见的方式。
    """
    EventLog(root).append(
        SYSTEM_SUBJECT,
        actor=ORCHESTRATOR,
        kind=LogKind.CONFIG_CHANGED,
        payload={
            "section": "init",
            "entrance": "cli",
            "rev": git_out(root, "rev-parse", "HEAD"),
        },
    )


def _commit(root: Path, message: str) -> None:
    git(root, *ORCHESTRATOR_IDENTITY, "commit", "-m", message)


#: 顶层仓还有本地提交没推上去。位于 tasks/ 下,不会污染版本化 Workspace。
WORKSPACE_PUSH_PENDING = paths.TASKS / "workspace-push-pending"


def configure_workspace_remote(root: Path, remote: str, branch: str | None = None) -> None:
    """为顶层 Workspace 配置唯一的 origin 并把当前状态发布出去。

    已有 origin 必须与表单一致；静默改指向可能把整个项目历史推到错误仓库。新建时推送
    失败会由 ``init_workspace`` 的事务边界清理本地骨架，认领时则保留目录并返回明确错误。
    """
    wanted = remote.strip()
    if not wanted:
        raise WorkspaceRemoteFailed("顶层项目仓库地址不能为空")
    current = git(root, "remote", "get-url", "origin", check=False)
    if current.returncode == 0 and current.stdout.strip():
        existing = current.stdout.strip()
        if existing != wanted:
            raise WorkspaceRemoteFailed(
                f"顶层 Workspace 已配置其他 origin:{existing}；拒绝改成 {wanted}"
            )
    else:
        git(root, "remote", "add", "origin", wanted)
    push_workspace_remote(root, branch)


def push_workspace_remote(root: Path, branch: str | None = None) -> None:
    """发布顶层 Workspace 当前 HEAD；失败留下可重试的持久标记。"""
    target_branch = branch or git_out(root, "branch", "--show-current")
    marker = root / WORKSPACE_PUSH_PENDING
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        git(root, "push", "-u", "origin", f"HEAD:{target_branch}")
    except GitError as error:
        # 这里只存状态。具体错误唯一留在事件面,避免记录平面出现两份会发散的正文。
        marker.write_text("pending", encoding="utf-8")
        raise WorkspaceRemoteFailed(
            "顶层项目仓库推送失败；确认仓库存在、当前凭证有写权限，且远端是空仓库。"
            f"\n  {error.stderr}"
        ) from error
    marker.unlink(missing_ok=True)


def workspace_push_pending(root: Path) -> bool:
    """顶层 Workspace 是否还有未发布的初始化提交。"""
    return (root / WORKSPACE_PUSH_PENDING).is_file()


def mark_workspace_push_pending(root: Path) -> None:
    """在产生待发布的挂载提交之前先持久化意图。"""
    marker = root / WORKSPACE_PUSH_PENDING
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("pending", encoding="utf-8")


def _mount(root: Path, repo: RepoSpec) -> None:
    args = ["submodule", "add"]
    if repo.branch:
        args += ["-b", repo.branch]
    args += [repo.url, repo.mount_point]
    try:
        git(root, *args, allow_file_protocol=is_local_url(repo.url))
    except GitError as error:
        raise MountFailed(repo, error) from error


# --- 延后挂载(界面建项目的异步那一半) ---------------------------------------

#: 挂载计划。住运行态目录(不进 git):它是"这个项目接入到哪一步了"的运行事实,
#: 挂载全部完成后它只剩历史价值。
MOUNT_PLAN = paths.TASKS / "mount-plan.json"


def write_mount_plan(root: Path, repos: list[RepoSpec]) -> None:
    target = root / MOUNT_PLAN
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {"url": item.url, "module_id": item.module_id, "path": item.path, "branch": item.branch}
        for item in repos
    ]
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_mount_plan(root: Path) -> tuple[RepoSpec, ...]:
    source = root / MOUNT_PLAN
    if not source.is_file():
        return ()
    payload = json.loads(source.read_text(encoding="utf-8"))
    return tuple(
        RepoSpec(
            url=item["url"],
            module_id=item["module_id"],
            path=item["path"],
            branch=item.get("branch"),
        )
        for item in payload
    )


def pending_mounts(root: Path) -> tuple[RepoSpec, ...]:
    """计划里还没挂上的仓。**从磁盘算,不存副本**——挂载点必须有可 checkout 的 HEAD。

    非空表示这个项目还在初始化:员工没有代码可读,研发任务的提交入口据此拒绝。
    没有挂载计划(CLI `agctl init` 建的、同步挂完的)自然是空,存量项目不受影响。
    """
    return tuple(item for item in read_mount_plan(root) if not _mount_is_checkoutable(root, item))


def workspace_initializing(root: Path) -> bool:
    """业务仓挂载或顶层远端同步任一未完成,项目就仍在初始化。"""
    return bool(pending_mounts(root)) or workspace_push_pending(root)


def _mount_is_checkoutable(root: Path, repo: RepoSpec) -> bool:
    """挂载点是否既有代码版本、又已进入父仓 HEAD，能随任务 worktree checkout。"""
    mount = root / repo.mount_point
    if not (mount / ".git").exists():
        return False
    if git(mount, "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        return False
    tracked = git(root, "ls-tree", "HEAD", "--", repo.mount_point, check=False)
    if tracked.returncode != 0 or not tracked.stdout.startswith("160000 commit "):
        return False
    configured = git(
        root,
        "config",
        "-f",
        ".gitmodules",
        "--get-regexp",
        r"^submodule\..*\.path$",
        check=False,
    )
    for line in configured.stdout.splitlines():
        key, separator, value = line.partition(" ")
        if not separator or value.strip().rstrip("/") != repo.mount_point.rstrip("/"):
            continue
        url = git(
            root,
            "config",
            "-f",
            ".gitmodules",
            "--get",
            f"{key.removesuffix('.path')}.url",
            check=False,
        )
        return url.returncode == 0 and url.stdout.strip() == repo.url
    return False


def unmounted_refusal(root: Path) -> str | None:
    """项目还在初始化时,研发任务提交入口该说的那句话。没在初始化返回 None。

    **REST 与 CLI 共用这一句**——各写一遍的话文案会分叉,而"两条入口同一句报错"正是
    这个仓库反复押注的纪律(先例:拓扑校验、需求查重)。
    """
    waiting = pending_mounts(root)
    if not waiting and not workspace_push_pending(root):
        return None
    if not waiting:
        return (
            "项目还在初始化:业务仓已挂载,但顶层项目仓库尚未同步。"
            "去基因组任务页查看失败原因并重试挂载收口。"
        )
    return (
        f"项目还在初始化:{len(waiting)} 个业务仓未挂载"
        f"({', '.join(item.module_id for item in waiting)}),员工还没有代码可读。"
        "去基因组任务页看挂载进度,失败的可以重试。"
    )


def mount_planned(root: Path, repo: RepoSpec) -> None:
    """挂一个计划里的仓并提交。逐仓一个提交:失败的仓不连累已挂上的。"""
    _mount(root, repo)
    resolution = resolve_verification(repo.module_id, root / repo.path)
    if isinstance(resolution, Ready):
        write_verification_spec(root, resolution.spec)
    elif isinstance(resolution, NeedsConfirmation):
        write_pending_verification(root, repo.module_id, resolution)
    git(root, "add", "-A")
    _commit(root, f"chore: 挂载业务仓 {repo.module_id}")
