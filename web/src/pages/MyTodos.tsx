/**
 * 我的待办:派给人的那些 Job。
 *
 * ## 它与「待我审批」不是一回事
 *
 * 审批看的是整个任务的 diff 并且**可以否决**;待办看的是**一份活**并且要交产物。混在一个
 * 列表里的话,"轮到你干活"与"轮到你拍板"会长得一样,而它们需要的注意力完全不同(见 ADR-0009
 * 的代价段)。
 *
 * ## 一张卡片要说清三件事
 *
 * **要干什么**(上下文包)、**要交什么**(产物契约:哪些字段必填)、**什么时候之前交**
 * (它现在处于提醒/改派的哪一段)。少了第二件,人会交一份"看起来对"的东西然后被校验打回;
 * 少了第三件,待办会在列表里慢慢烂掉。
 *
 * ## 没有"跳过校验"按钮
 *
 * 交上去的产物过与硅基员工**完全相同**的校验,失败给同一份错误并允许重交。有那个按钮的话,
 * "人也是一种运行时"就只剩一半:流水线仍然会因为执行者是人而降低标准。
 */
import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  api,
  type TodoDetail,
  type TodoItem,
} from "../api/client";
import { Card, Empty, Note, Tag } from "../ui/kit";

/** 这张待办要人去哪儿干活。 */
const KIND_LABEL: Record<string, string> = {
  artifact: "在这里交产物",
  worktree: "去工作树里改代码",
  split: "裁决拆分提案",
};

export function MyTodos({ assignee = "" }: { assignee?: string }) {
  const [items, setItems] = useState<TodoItem[] | null>(null);
  const [open, setOpen] = useState<TodoDetail | null>(null);
  const [error, setError] = useState("");

  const reload = useCallback(() => {
    api
      .todos(assignee)
      .then((page) => {
        setItems(page.items);
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
  }, [assignee]);

  useEffect(() => reload(), [reload]);

  if (error) return <Note tone="warn">拉不到待办:{error}</Note>;
  if (!items) return <Empty>加载中…</Empty>;

  return (
    <Card title="我的待办" extra={<span className="mono">{items.length} 张</span>}>
      {items.length === 0 ? (
        <Empty>没有派给我的活。</Empty>
      ) : (
        <table>
          <tbody>
            {items.map((todo) => (
              <tr key={todo.id} onClick={() => api.todo(todo.id).then(setOpen)}>
                <td>
                  {todo.procedure_id}
                  <div className="mono">
                    {todo.task_id}/{todo.stage}
                  </div>
                </td>
                <td>
                  <Tag tone={todo.kind === "worktree" ? "pur" : "mute"}>
                    {KIND_LABEL[todo.kind] ?? todo.kind}
                  </Tag>
                </td>
                <td className="mono">
                  {/* **截止时间要在列表里。** 没有它的待办会在列表里慢慢烂掉,而那正是
                      三段机制要防的事。 */}
                  {todo.due_at ? todo.due_at.slice(0, 10) : "—"}
                </td>
                <td style={{ textAlign: "right" }}>
                  {/* 改派过就说清楚:它已经在别人手里放过一轮了,这一轮的窗口是重新算的。 */}
                  {todo.reassignments > 0 ? (
                    <Tag tone="warn">已改派 {todo.reassignments} 次</Tag>
                  ) : todo.reminded ? (
                    <Tag tone="warn">提醒过了</Tag>
                  ) : (
                    <Tag tone="pur">待办</Tag>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && open.kind === "split" && (
        <SplitVerdict todo={open} onDone={() => { setOpen(null); reload(); }} />
      )}
      {open && open.kind !== "split" && (
        <HandIn todo={open} onDone={() => { setOpen(null); reload(); }} />
      )}
    </Card>
  );
}

/** 一份等着裁决的拆分提案的形状(TodoItem.proposal)。 */
type SplitProposal = {
  children?: { title?: string; text?: string; blocked_by?: number[] }[];
  rationale?: string;
};

/** 编辑态里的一格子需求。依赖以逗号分隔的批内序号编辑,提交时解析成整数。 */
type ChildDraft = { title: string; text: string; deps: string };

/**
 * 拆分提案的裁决卡:只读的提案 + 确认 / 调整后确认 / 打回。
 *
 * **只有动过才提交调整稿**:原样确认交的是 `{approved: true}`,后端落的是提案本身——
 * "人看过没改"与"人改成了一样的内容"要分得开,审计读的是前者。
 *
 * **打回必须带反馈。** 反馈会以失败报告回注下一轮解析;没有理由的打回,下一轮多半
 * 原样再来一份,白烧一轮 token。
 */
export function SplitVerdict({ todo, onDone }: { todo: TodoDetail; onDone: () => void }) {
  const [feedback, setFeedback] = useState("");
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  const proposal = (todo.proposal ?? {}) as SplitProposal;
  const children = proposal.children ?? [];
  const [editing, setEditing] = useState(false);
  const [drafts, setDrafts] = useState<ChildDraft[]>(() =>
    children.map((child) => ({
      title: child.title ?? "",
      text: child.text ?? "",
      deps: (child.blocked_by ?? []).join(","),
    })),
  );

  const decide = (payload: Record<string, unknown>) => {
    setBusy(true);
    api
      .submitTodo(todo.id, payload)
      .then((response) => {
        setBusy(false);
        if (response.ok) onDone();
        else setDetail(response.detail);
      })
      .catch((e: ApiError) => {
        setBusy(false);
        setDetail(e.detail);
      });
  };

  const editedBatch = () =>
    drafts.map((draft) => ({
      title: draft.title,
      text: draft.text,
      blocked_by: draft.deps
        .split(",")
        .map((item) => item.trim())
        .filter((item) => item !== "")
        .map(Number)
        .filter((item) => Number.isInteger(item)),
    }));

  return (
    <div style={{ marginTop: 12 }}>
      <div className="m">
        <b>拆分提案</b> · {todo.task_id}
      </div>
      {proposal.rationale && <div className="m">为什么拆:{proposal.rationale}</div>}
      {!editing ? (
        <table>
          <tbody>
            {children.map((child, index) => (
              <tr key={index}>
                <td className="mono">#{index}</td>
                <td>
                  <b>{child.title}</b>
                  <div className="m">{child.text}</div>
                </td>
                <td className="mono">
                  {(child.blocked_by ?? []).length > 0
                    ? `依赖 ${(child.blocked_by ?? []).map((i) => `#${i}`).join("、")}`
                    : "无前置"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div>
          {drafts.map((draft, index) => (
            <div key={index} style={{ display: "flex", gap: 8, marginBottom: 8 }}>
              <span className="mono">#{index}</span>
              <div style={{ flex: 1 }}>
                <input
                  aria-label={`子需求 ${index} 标题`}
                  style={{ width: "100%" }}
                  value={draft.title}
                  onChange={(event) =>
                    setDrafts(drafts.map((item, at) =>
                      at === index ? { ...item, title: event.target.value } : item,
                    ))
                  }
                />
                <textarea
                  aria-label={`子需求 ${index} 需求全文`}
                  style={{ width: "100%", minHeight: 48 }}
                  value={draft.text}
                  onChange={(event) =>
                    setDrafts(drafts.map((item, at) =>
                      at === index ? { ...item, text: event.target.value } : item,
                    ))
                  }
                />
                <input
                  aria-label={`子需求 ${index} 依赖`}
                  placeholder="依赖的批内序号,逗号分隔,如 0,2"
                  value={draft.deps}
                  onChange={(event) =>
                    setDrafts(drafts.map((item, at) =>
                      at === index ? { ...item, deps: event.target.value } : item,
                    ))
                  }
                />
              </div>
              <button
                className="btn"
                onClick={() => setDrafts(drafts.filter((_, at) => at !== index))}
              >
                删除
              </button>
            </div>
          ))}
          <button
            className="btn"
            onClick={() => setDrafts([...drafts, { title: "", text: "", deps: "" }])}
          >
            增加子需求
          </button>
        </div>
      )}
      <textarea
        aria-label="打回的反馈"
        className="log"
        style={{ width: "100%", minHeight: 60 }}
        placeholder="为什么不该这么拆?反馈会进下一轮解析的上下文。"
        value={feedback}
        onChange={(event) => setFeedback(event.target.value)}
      />
      {detail && <Note tone="warn">没有交上去:{detail}</Note>}
      <div style={{ display: "flex", gap: 8 }}>
        <button
          className="btn pri"
          disabled={busy}
          onClick={() =>
            decide(editing ? { approved: true, children: editedBatch() } : { approved: true })
          }
        >
          {busy ? "处理中…" : "确认拆分"}
        </button>
        {!editing && (
          <button className="btn" onClick={() => setEditing(true)}>
            调整提案
          </button>
        )}
        <button
          className="btn"
          disabled={busy || feedback.trim() === ""}
          onClick={() => decide({ approved: false, feedback: feedback.trim() })}
        >
          {busy ? "打回中…" : "打回提案"}
        </button>
      </div>
    </div>
  );
}

export function HandIn({ todo, onDone }: { todo: TodoDetail; onDone: () => void }) {
  const [draft, setDraft] = useState("{\n  \n}");
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  const required = ((todo.schema?.required as string[] | undefined) ?? []).join("、");

  return (
    <div style={{ marginTop: 12 }}>
      <div className="m">
        <b>{todo.procedure_id}</b> · {todo.task_id}/{todo.stage}
      </div>
      <div className="kv">
        <span className="k">上下文包</span>
        <span className="v mono">{todo.context_file}</span>
      </div>
      {todo.kind === "worktree" && (
        <div className="kv">
          <span className="k">去这里改代码</span>
          <span className="v mono">{todo.workdir}</span>
        </div>
      )}
      {/* **"要交什么"必须写出来。** 不写的话,人会交一份看起来对的东西然后被打回,
          而那份挫败感与"这套系统不好用"是同一个东西。 */}
      <div className="kv">
        <span className="k">必填字段</span>
        <span className="v mono">{required || "(无)"}</span>
      </div>
      <textarea
        aria-label="产物 JSON"
        className="log"
        style={{ width: "100%", minHeight: 120 }}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
      />
      {detail && (
        <Note tone="warn">
          产物不合契约,没有交上去:{detail}
        </Note>
      )}
      <button
        className="btn pri"
        disabled={busy}
        onClick={() => {
          let parsed: Record<string, unknown>;
          try {
            parsed = JSON.parse(draft);
          } catch {
            setDetail("这不是合法的 JSON");
            return;
          }
          setBusy(true);
          api
            .submitTodo(todo.id, parsed)
            .then((response) => {
              setBusy(false);
              if (response.ok) onDone();
              else setDetail(response.detail);
            })
            .catch((e: ApiError) => {
              setBusy(false);
              setDetail(e.detail);
            });
        }}
      >
        {busy ? "交付中…" : "交付"}
      </button>
    </div>
  );
}
