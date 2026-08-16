/**
 * 应用骨架。
 *
 * **没有任何绕过 REST 的私有通道。** 命令行与网页走完全相同的接口——两条路各走各的实现,
 * 它们会慢慢分叉,而分叉一旦形成就很难收回。
 *
 * 身份现在是顶栏里的一个输入框。真正的登录在 PRD 18(SSO + RBAC),但**审批的身份校验
 * 已经在服务端**——这里换的只是身份来源,不是校验位置。
 */
import { useCallback, useEffect, useState } from "react";
import { api, getWorkspace, setWorkspace, type WorkspaceCreated, type WorkspaceEntry } from "./api/client";
import { ProjectSetup } from "./pages/ProjectSetup";
import { ProjectCreated } from "./pages/ProjectCreated";
import { ChatDrawer } from "./chat/Drawer";
import { ApprovalCenter } from "./pages/ApprovalCenter";
import { Chat } from "./pages/Chat";
import { GenomeManagement } from "./pages/GenomeManagement";
import { Observability } from "./pages/Observability";
import { Requirements } from "./pages/Requirements";
import { Roster } from "./pages/Roster";
import { Settings } from "./pages/Settings";
import { Submit } from "./pages/Submit";
import { TaskCenter } from "./pages/TaskCenter";
import { Workbench } from "./pages/Workbench";
import { TopBar } from "./ui/TopBar";
import { navigate, parseHash, routeForPage, type PageId, type Route } from "./routing";
import { Empty } from "./ui/kit";
import { subscribe } from "./api/live";

const NAV = [
  ["work", "🏠 工作台"],
  ["submit", "📝 需求提交"],
  ["requirements", "🗂 需求管理"],
  ["tasks", "📋 任务中心"],
  ["review", "✅ 审批中心"],
  ["chat", "💬 对话工作台"],
  ["genome", "🧬 基因组管理"],
  ["roster", "👥 员工管理"],
  ["settings", "⚙️ 系统设置"],
  ["insights", "📈 观测中心"],
] as const;

export function App() {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  const [actor, setActor] = useState("");
  const [pending, setPending] = useState(0);
  // 项目清单与当前项目。**当前项目的真身在 `api/client` 里**(每个请求从那里取头),
  // 这里的 state 只是它的展示镜像——两处都当真身的话,迟早一处切了另一处没切。
  const [projects, setProjects] = useState<WorkspaceEntry[]>([]);
  const [workspace, setWorkspaceState] = useState(getWorkspace());
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [createdWorkspace, setCreatedWorkspace] = useState<WorkspaceCreated | null>(null);

  useEffect(() => {
    const syncRoute = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", syncRoute);
    window.addEventListener("popstate", syncRoute);
    if (!window.location.hash) navigate({ page: "work" }, true);
    return () => {
      window.removeEventListener("hashchange", syncRoute);
      window.removeEventListener("popstate", syncRoute);
    };
  }, []);

  const page = route.page;
  const openId = route.page === "tasks" && "taskId" in route ? route.taskId : null;
  const openRequirementId =
    route.page === "requirements" && "requirementId" in route ? route.requirementId : null;

  useEffect(() => {
    if (
      route.page === "project-created" &&
      projects.some((project) => project.name === route.workspace) &&
      workspace !== route.workspace
    ) {
      setWorkspace(route.workspace);
      setWorkspaceState(route.workspace);
    }
  }, [projects, route, workspace]);

  const go = useCallback((nextPage: string) => {
    navigate(routeForPage(nextPage as PageId));
  }, []);

  const reloadProjects = () => {
    api
      .workspaces()
      .then((found) => {
        // `entries` 缺失或为空时退回 `items`(老形状只有名字):零项目的判断不能建立在
        // "服务端够新"上,否则升级窗口里整站变成"创建第一个项目"。
        const entries: WorkspaceEntry[] = found.entries?.length
          ? found.entries
          : (found.items ?? []).map((name) => ({ name, initializing: false }));
        setProjects(entries);
        setProjectsLoaded(true);
        // 存的选择已失效(项目被注销/首次使用)时落到第一个——不落的话,每个请求都
        // 带着一个服务端不认的名字,整站 404。
        const fallback = entries[0];
        if (fallback && !entries.some((item) => item.name === getWorkspace())) {
          // 首次解析到唯一可用项目不是一次用户切换；保留 URL 中的详情，让深链接能启动。
          setWorkspace(fallback.name);
          setWorkspaceState(fallback.name);
        }
      })
      .catch(() => setProjectsLoaded(true));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(reloadProjects, []);

  const switchWorkspace = (name: string) => {
    setWorkspace(name);
    setWorkspaceState(name);
    // 换血:一级页可保留,对象详情属于上一个项目,带过去只会 404。
    if (route.page === "tasks" && "taskId" in route) navigate({ page: "tasks" }, true);
    if (route.page === "requirements" && "requirementId" in route) {
      navigate({ page: "requirements" }, true);
    }
    if (route.page === "project-created") navigate({ page: "work" }, true);
  };

  // 待审批数从**已有的任务列表**算,不新增端点。它随页面切换重算——审批完了徽章还挂着
  // 的话,那个红点会训练用户忽略它。
  useEffect(() => {
    const reloadPending = () => api
      .tasks()
      .then((found) => setPending(found.filter((task) => task.state === "REVIEWING").length))
      .catch(() => setPending(0));
    reloadPending();
    return subscribe(reloadPending);
  }, [page, workspace]);

  const open = (id: string) => {
    navigate({ page: "tasks", taskId: id });
  };

  const openRequirement = (id: string) => {
    navigate({ page: "requirements", requirementId: id });
  };

  // 零项目 → 全屏引导;建成后刷新清单并切过去。加载完成前不渲染引导——一闪而过的
  // "创建第一个项目"会把一次普通的慢网络变成一次心跳骤停。
  if (projectsLoaded && projects.length === 0 && route.page === "project-created") {
    return (
      <div className="main-wrap">
        <main className="main">
          <Empty>
            <b>找不到项目 {route.workspace}</b>
            <button className="btn pri" onClick={() => navigate({ page: "new-project" }, true)}>
              创建第一个项目
            </button>
          </Empty>
        </main>
      </div>
    );
  }
  if (projectsLoaded && projects.length === 0) {
    return (
      // **不是 `.app`**:那是"196px 侧栏 + 主区"的网格,这一页没有侧栏,套上它内容会
      // 整个掉进侧栏那一列。引导页只有一个居中的主区。
      <div className="main-wrap">
        <main className="main">
          <div style={{ maxWidth: 760, margin: "0 auto" }}>
            <ProjectSetup
              first
              onCreated={(created) => {
                setCreatedWorkspace(created);
                switchWorkspace(created.name);
                reloadProjects();
                navigate({ page: "project-created", workspace: created.name });
              }}
            />
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="app">
      <aside className="side">
        <div className="logo"><i />AgentGenome</div>
        <nav className="nav">
          {NAV.map(([id, label]) => (
            <button key={id} className={page === id ? "on" : ""} onClick={() => go(id)}>
              {label}
              {/* 不点进去就知道有没有积压。 */}
              {id === "review" && pending > 0 && <span className="n">{pending}</span>}
            </button>
          ))}
        </nav>
        {/* **身份不在这里了。** 它搬去了顶栏——两处都放的话,用户会问哪个才算数。 */}
      </aside>

      <div className="main-wrap">
        <TopBar
          actor={actor}
          onActor={setActor}
          projects={projects}
          current={workspace || projects[0]?.name || ""}
          onSwitch={switchWorkspace}
          onNewProject={() => go("new-project")}
        />
        {/* key=workspace:切项目即整页换血——所有页面自己拉数据,重挂载比逐页通知干净。 */}
        <main className="main" key={workspace}>
          {page === "new-project" && (
            <ProjectSetup
              first={false}
              onCreated={(created) => {
                setCreatedWorkspace(created);
                switchWorkspace(created.name);
                reloadProjects();
                navigate({ page: "project-created", workspace: created.name });
              }}
            />
          )}
          {page === "project-created" && (
            projectsLoaded && !projects.some((project) => project.name === route.workspace) ? (
              <Empty>
                <b>找不到项目 {route.workspace}</b>
                <button className="btn pri" onClick={() => go("work")}>返回工作台</button>
              </Empty>
            ) : workspace === route.workspace ? (
              <ProjectCreated
                workspace={route.workspace}
                initial={createdWorkspace?.name === route.workspace ? createdWorkspace : null}
              />
            ) : <Empty>正在切换到项目 {route.workspace}…</Empty>
          )}
          {page === "work" && <Workbench onOpen={open} onGo={go} actor={actor} />}
          {/* 提交成功去需求详情,不是任务详情:提的是需求,任务只是它的第一次尝试。
              存量任务没有需求(requirement_id 为 null)时才退回任务详情。 */}
          {page === "submit" && (
            <Submit
              onSubmitted={(task) =>
                task.requirement_id ? openRequirement(task.requirement_id) : open(task.id)
              }
              // 初始化中的项目提不了任务:禁用入口并说明原因,比点了提交才 409 早一步。
              disabledReason={
                projects.find((item) => item.name === (workspace || projects[0]?.name))
                  ?.initializing
                  ? "项目还在初始化:业务仓挂载中,员工还没有代码可读。进度在任务中心的基因组任务看板。"
                  : undefined
              }
            />
          )}
          {page === "requirements" && (
            <Requirements
              openId={openRequirementId}
              onOpen={(id) => id ? openRequirement(id) : navigate({ page: "requirements" })}
              onOpenTask={open}
            />
          )}
          {page === "tasks" && (
            <TaskCenter
              openId={openId}
              onOpen={(id) => id ? open(id) : navigate({ page: "tasks" })}
              onOpenRequirement={openRequirement}
            />
          )}
          {page === "review" && <ApprovalCenter actor={actor} />}
          {page === "chat" && <Chat onOpenTask={open} />}
          {page === "genome" && (
            <GenomeManagement
              tab={"tab" in route ? route.tab : undefined}
              onTab={(tab) => navigate({ page: "genome", tab })}
            />
          )}
          {page === "roster" && <Roster />}
          {page === "settings" && <Settings />}
          {page === "insights" && (
            <Observability
              tab={"tab" in route ? route.tab : undefined}
              onTab={(tab) => navigate({ page: "insights", tab })}
              actor={actor}
            />
          )}
          {page === "not-found" && (
            <Empty>
              <b>这个控制台地址无效</b>
              <div className="m">{route.requested}</div>
              <button className="btn pri" onClick={() => go("work")}>返回工作台</button>
            </Empty>
          )}
        </main>
      </div>

      {/* 抽屉与页面切换正交:随处 ⌘J 唤起,自动携带当前页面的上下文。 */}
      <ChatDrawer page={page} openTaskId={openId} />
    </div>
  );
}
