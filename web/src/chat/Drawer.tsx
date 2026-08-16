/**
 * 全局对话抽屉:随处可问。
 *
 * ## ⌘J,不是 ⌘K
 *
 * `styles.css` 的 `.search kbd` 已经把 ⌘K 给了顶栏搜索。两个功能抢同一个键位的结果是
 * **先注册的赢、后注册的静默失效**,属于最难查的一类前端 bug——没有报错,只有"我按了
 * 没反应"。搜索框那句"或输入 / 快捷指令"本来就留了第二条入口;真想合并的话应该是搜索
 * 面板增加对话模式,而不是抢键。
 *
 * ## 抽屉自动携带当前页面的上下文
 *
 * 任务页唤起 = 关联该任务;基因组页唤起 = 找架构员工。用户不必重复说明自己在看什么
 * ——而"我在看什么"这件事页面本来就知道。
 *
 * ## 抽屉里的会话一律只读
 *
 * 它的定位是"就地问一句",不提供改代码的开关。想边聊边改去工作台建——那里才有空间
 * 把"改动落在哪、怎么回主线"说清楚,而一个浮在角落里的抽屉没有。
 */
import { useEffect, useState } from "react";
import { ApiError, type BlockItem, type SessionSummary } from "../api/client";
import { Empty, Note, Tag } from "../ui/kit";
import { BlockView } from "./blocks";
import { createSession } from "./newSession";
import { useSession } from "./useSession";

/** 页面 → 默认员工与关联任务。**一份映射**,抽屉与工作台共用同一套判断。 */
export function contextFor(page: string, openTaskId: string | null): {
  employee: string;
  task_id?: string;
} {
  if (page === "tasks" && openTaskId) {
    // 在看一个任务时唤起,想问的几乎一定是"这个任务为什么这么干"。
    return { employee: "dev-employee", task_id: openTaskId };
  }
  if (page === "genome") {
    return { employee: "arch-employee" };
  }
  return { employee: "dev-employee" };
}

export function ChatDrawer({
  page,
  openTaskId,
}: {
  page: string;
  openTaskId: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [session, setSession] = useState<SessionSummary | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      // ⌘J / Ctrl-J。**不碰 ⌘K** —— 那是搜索的。
      if (event.key.toLowerCase() === "j" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!open || session) return;
    const wanted = contextFor(page, openTaskId);
    // **与工作台走同一段创建逻辑**,抽屉只是把员工与模式预填好。两套实现三个月后必然
    // 分叉,理由与共享组件抽取那一条完全相同。
    createSession({
      employee: wanted.employee,
      // 抽屉一律只读,见模块文档。
      writable: false,
      title: "",
      taskId: wanted.task_id ?? "",
    })
      .then(setSession)
      .catch((e: ApiError) => setError(e.detail));
  }, [open, page, openTaskId, session]);

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-label="对话抽屉"
      style={{
        position: "fixed",
        right: 16,
        bottom: 16,
        width: 460,
        maxHeight: "76vh",
        overflow: "auto",
        background: "#fff",
        border: "1px solid var(--pri)",
        borderRadius: 10,
        padding: "14px 16px",
        boxShadow: "0 12px 36px rgba(16,24,40,.18)",
        zIndex: 30,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
        <b>对话</b>
        {session && <Tag tone={session.writable ? "warn" : "pri"}>{session.writable ? "可写" : "只读"}</Tag>}
        <button className="btn sm" style={{ marginLeft: "auto" }} onClick={() => setOpen(false)}>
          关闭
        </button>
      </div>
      {error && <Note tone="warn">{error}</Note>}
      {session ? <DrawerBody sessionId={session.id} /> : <Empty>正在开会话…</Empty>}
    </div>
  );
}

function DrawerBody({ sessionId }: { sessionId: string }) {
  const { blocks, state, error, send, retry } = useSession(sessionId);
  const [draft, setDraft] = useState("");
  const busy = state === "sending" || state === "streaming" || state === "tool-running";

  return (
    <>
      <div style={{ maxHeight: "48vh", overflow: "auto" }}>
        {blocks.length === 0 ? (
          <Empty>问点什么</Empty>
        ) : (
          blocks.map((block) => <DrawerBlock key={block.seq} block={block} />)
        )}
      </div>
      {state === "error" && (
        <Note tone="warn">
          {error} <button className="btn sm" onClick={() => void retry()}>重试</button>
        </Note>
      )}
      <textarea
        aria-label="抽屉输入"
        placeholder="问点什么……"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
      />
      <button
        className="btn sm pri"
        disabled={busy}
        style={{ marginTop: 6 }}
        onClick={() => {
          const message = draft;
          setDraft("");
          void send(message);
        }}
      >
        发送
      </button>
    </>
  );
}

function DrawerBlock({ block }: { block: BlockItem }) {
  const mine = (block.detail as Record<string, unknown> | undefined)?.["role"] === "user";
  if (mine) {
    return (
      <div style={{ textAlign: "right", margin: "0 0 8px" }}>
        <span style={{ background: "var(--pri-bg)", borderRadius: 8, padding: "6px 10px" }}>
          {block.text}
        </span>
      </div>
    );
  }
  return (
    <div style={{ margin: "0 0 8px" }}>
      <BlockView block={block} />
    </div>
  );
}
