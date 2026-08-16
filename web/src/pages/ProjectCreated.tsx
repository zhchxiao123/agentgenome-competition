import { useEffect, useState } from "react";
import { ApiError, api, type GenomeTaskSummary, type WorkspaceCreated } from "../api/client";
import { ContainerOnboarding } from "../components/ContainerOnboarding";
import { Card, Empty, Note, Tag } from "../ui/kit";

export function ProjectCreated({
  workspace,
  initial,
}: {
  workspace: string;
  initial: WorkspaceCreated | null;
}) {
  const [mount, setMount] = useState<GenomeTaskSummary | null>(null);
  const [adopted, setAdopted] = useState<boolean | null>(initial?.adopted ?? null);
  const [loading, setLoading] = useState(initial === null);
  const [error, setError] = useState("");
  const [initStarted, setInitStarted] = useState("");

  useEffect(() => {
    api.genomeTasks({ kind: "mount" })
      .then((page) => {
        setMount(page.items[0] ?? null);
        setLoading(false);
      })
      .catch((reason: ApiError) => {
        setError(reason.detail);
        setLoading(false);
      });
    if (initial === null) {
      api.auditEvents({ kind: "workspace_changed" })
        .then((audit) => {
          const creation = audit.items.find((item) => item.payload?.name === workspace);
          if (creation?.payload?.action === "adopt") setAdopted(true);
          else if (creation?.payload?.action === "create") setAdopted(false);
        })
        // 创建/认领文案允许降级成中性状态；审计权限不能阻断挂载事实与下一步入口。
        .catch(() => setAdopted(null));
    }
  }, [workspace]);

  const mountId = initial?.mount_task_id ?? mount?.id ?? null;

  return (
    <>
      <h1>项目 {workspace} {adopted === null ? "创建结果" : adopted ? "已重新接入" : "已创建"}</h1>
      <div className="sub">创建结果保留在这个地址；刷新或切换项目不会抹掉下一步。</div>
      <Card title="接下来">
        {/* 配了容器运行时却一个员工都没供应时,这里给下一步(PRD 33)。 */}
        <ContainerOnboarding />
        {loading ? <Empty>正在恢复挂载状态…</Empty> : mountId ? (
          <Note>
            挂载 Genome task <span className="mono">{mountId}</span>
            {mount && <> · <Tag>{mount.state}</Tag></>}
          </Note>
        ) : (
          <Note>没有运行中的挂载任务；业务仓可能已经就绪。</Note>
        )}
        <p>挂载完成后发起知识初始化，让架构员工扫描仓库、确认边界并逐模块深读。</p>
        {initStarted ? (
          <Note>知识初始化已发起（Genome task <span className="mono">{initStarted}</span>）。</Note>
        ) : (
          <button
            className="btn pri"
            onClick={() => api.knowledgeInit()
              .then((task) => { setInitStarted(task.id); setError(""); })
              .catch((reason: ApiError) => setError(reason.detail))}
          >
            发起知识初始化
          </button>
        )}
        {error && (
          <Note tone="warn">
            {error} <a href="#/tasks">去任务中心查看失败原因或重试</a>
          </Note>
        )}
      </Card>
    </>
  );
}
