/**
 * 建项目:从零到一不碰终端。
 *
 * 零项目时它是首屏引导;有项目时从切换器的「＋ 新项目」进来。骨架同步就位(提交即出现在
 * 切换器里),clone 走 MOUNT 基因组任务异步跑——进度与失败在基因组任务页,这里只负责发起
 * 和把下一步(知识初始化)递到手边。
 */
import { useRef, useState } from "react";
import { ApiError, api, type WorkspaceCreated } from "../api/client";
import { Card, Note } from "../ui/kit";

export function ProjectSetup({
  first,
  onCreated,
}: {
  /** 这是不是第一个项目——只影响文案,不影响行为。 */
  first: boolean;
  onCreated: (created: WorkspaceCreated) => void;
}) {
  const [name, setName] = useState("");
  const [workspaceRepo, setWorkspaceRepo] = useState("");
  const [repos, setRepos] = useState("");
  const [error, setError] = useState("");
  const [created, setCreated] = useState<WorkspaceCreated | null>(null);
  const [initStarted, setInitStarted] = useState("");
  const [creating, setCreating] = useState(false);
  const creatingRef = useRef(false);

  const submit = () => {
    if (creatingRef.current) return;
    const lines = repos
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (!name.trim() || !workspaceRepo.trim() || lines.length === 0) {
      setError(
        "项目名、顶层项目仓库地址与至少一个业务仓库地址都要填——Workspace 是独立的协作仓。",
      );
      return;
    }
    creatingRef.current = true;
    setCreating(true);
    setError("");
    api
      .createWorkspace({
        name: name.trim(),
        workspace_repo: workspaceRepo.trim(),
        repos: lines,
      })
      .then((found) => {
        setError("");
        setCreated(found);
        onCreated(found);
      })
      .catch((e: ApiError) => {
        creatingRef.current = false;
        setCreating(false);
        setError(e.detail);
      });
  };

  const startKnowledgeInit = () => {
    api
      .knowledgeInit()
      .then((task) => setInitStarted(task.id))
      .catch((e: ApiError) => setError(e.detail));
  };

  if (created) {
    return (
      <>
        <h1>项目 {created.name} {created.adopted ? "已重新接入" : "已创建"}</h1>
        <div className="sub">
          {created.adopted
            ? "这个名字的项目目录已经存在(多半是重启前建过的),已认领——目录里的东西没有被动过"
            : "骨架已就位;业务仓在后台挂载中"}
        </div>
        <Card title="接下来">
          {created.mount_task_id ? (
            <Note>
              挂载进度在「任务中心」的基因组任务看板(任务 <span className="mono">{created.mount_task_id}</span>)。
              单个仓失败不影响其余仓,失败原因在异常队列里,修好后可重试。
            </Note>
          ) : (
            <Note>业务仓已就绪,可以直接发起知识初始化。</Note>
          )}
          <p>
            挂载完成后,发起<b>知识初始化</b>让架构员工读懂这个项目——扫描、边界确认、逐模块
            深读都在界面上走完。
          </p>
          {initStarted ? (
            <Note>知识初始化已发起(任务 <span className="mono">{initStarted}</span>),去基因组任务看板确认边界草案。</Note>
          ) : (
            <button className="btn pri" onClick={startKnowledgeInit}>
              发起知识初始化
            </button>
          )}
          {error && <Note tone="warn">{error}</Note>}
        </Card>
      </>
    );
  }

  return (
    <>
      <h1>{first ? "创建第一个项目" : "新项目"}</h1>
      <div className="sub">
        {first
          ? "这个实例上还没有任何项目。填项目仓库与业务仓地址,从这里开始。"
          : "项目仓库保存治理资产,业务仓保存代码；骨架立刻就位,clone 在后台跑"}
      </div>

      <Card>
        <label>项目名 <span className="req">*</span></label>
        <input
          value={name}
          disabled={creating}
          onChange={(e) => setName(e.target.value)}
          placeholder="字母数字与 -/_,会成为切换器里的名字"
        />

        <label>顶层项目仓库地址 <span className="req">*</span></label>
        <input
          value={workspaceRepo}
          disabled={creating}
          onChange={(event) => setWorkspaceRepo(event.target.value)}
          placeholder="https://github.com/org/agentgenome-workspace.git"
        />
        <span className="hint">请使用空仓库；这里保存门禁、项目知识与业务仓子模块指针。</span>

        <label>
          业务仓地址 <span className="req">*</span>{" "}
          <span className="hint">· 一行一个,`地址@分支` 指定分支</span>
        </label>
        <textarea
          value={repos}
          disabled={creating}
          onChange={(e) => setRepos(e.target.value)}
          placeholder={"git@github.com:org/order-service.git\nhttps://github.com/org/inventory.git@release"}
        />
        <Note>
          顶层仓库需要写权限,业务仓需要读写权限；操作使用<b>服务器本机的 git 凭证</b>。
          私有仓认证失败会进入异常队列,配置好凭证后可以重试。
        </Note>

        {error && <Note tone="warn">{error}</Note>}
        <button className="btn pri" onClick={submit} disabled={creating}>
          {creating ? "创建中…" : "创建项目"}
        </button>
      </Card>
    </>
  );
}
