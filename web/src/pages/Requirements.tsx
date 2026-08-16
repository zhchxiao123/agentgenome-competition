/**
 * 需求管理:提的那件事怎么样了,一眼看到。
 *
 * 状态是服务端从尝试链**算出来的**,这一页只负责摆出来——前端不重新推导一遍,推导规则
 * 改了不该有第二处要跟着改。「再试一次」就地展开:重试的人手里通常已经有上一轮的教训,
 * 让他跳回提交页重填一遍,等于把教训丢在路上。
 */
import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  api,
  type RequirementDetail,
  type RequirementSummary,
} from "../api/client";
import { subscribe } from "../api/live";
import { priorityLabel } from "../priority";
import { Card, Empty, Note, Tag } from "../ui/kit";
import { taskDisplayLabel } from "./taskPresentation";

const STATE_LABEL: Record<string, string> = {
  queued: "排队中",
  in_progress: "进行中",
  delivered: "已交付",
  parked: "已搁置",
};

const STATE_TONE: Record<string, "ok" | "pur" | "warn" | "mute"> = {
  queued: "mute",
  in_progress: "pur",
  delivered: "ok",
  parked: "warn",
};

const FILTERS = ["all", "queued", "in_progress", "delivered", "parked"] as const;

export function Requirements({
  openId,
  onOpen,
  onOpenTask,
}: {
  openId: string | null;
  onOpen: (id: string | null) => void;
  onOpenTask: (id: string) => void;
}) {
  const [items, setItems] = useState<RequirementSummary[]>([]);
  const [filter, setFilter] = useState<string>("all");
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const reload = useCallback(() => {
    api
      .requirements()
      .then((found) => {
        setItems(found);
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
  }, []);

  useEffect(() => {
    reload();
    return subscribe(reload);
  }, [reload]);

  // 顶层不被一长串子需求刷屏:默认收进母需求名下,展开才见(PRD 48 D8)。
  const childrenOf = (parentId: string) => items.filter((item) => item.parent_id === parentId);
  const shown = items.filter(
    (item) => !item.parent_id && (filter === "all" || item.state === filter),
  );

  return (
    <>
      <h1>需求管理</h1>
      <div className="sub">需求是承载「再试一次」的容器;每次尝试是一个一次性的研发任务</div>

      {error && <Note tone="warn">拉不到需求列表:{error}</Note>}

      <div style={{ display: "flex", gap: 8, margin: "12px 0" }}>
        {FILTERS.map((key) => (
          <button
            key={key}
            className={`btn ${filter === key ? "pri" : ""}`}
            onClick={() => setFilter(key)}
          >
            {key === "all" ? "全部" : STATE_LABEL[key]}
          </button>
        ))}
      </div>

      {shown.length === 0 ? (
        <Empty>{items.length === 0 ? "还没有需求 —— 去「需求提交」发起第一个" : "这个状态下没有需求"}</Empty>
      ) : (
        <Card>
          {shown.map((item) => (
            <div key={item.id}>
              <div
                className="kv"
                style={{ cursor: "pointer" }}
                onClick={() => onOpen(item.id)}
              >
                <span className="k mono">{item.id}</span>
                <span className="v" style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <Tag tone={STATE_TONE[item.state] ?? "mute"}>
                    {STATE_LABEL[item.state] ?? item.state}
                  </Tag>
                  <b>{item.title}</b>
                  <span className="m">尝试 {item.attempts} 次 · {priorityLabel(item.priority)}</span>
                  {item.children_total > 0 && (
                    <>
                      <Tag tone="pur">
                        {item.children_delivered}/{item.children_total} 子需求
                      </Tag>
                      <button
                        className="btn"
                        onClick={(event) => {
                          event.stopPropagation();
                          setExpanded({ ...expanded, [item.id]: !expanded[item.id] });
                        }}
                      >
                        {expanded[item.id] ? "收起子需求" : "展开子需求"}
                      </button>
                    </>
                  )}
                </span>
              </div>
              {expanded[item.id] &&
                childrenOf(item.id).map((child) => (
                  <div
                    key={child.id}
                    className="kv"
                    style={{ cursor: "pointer", paddingLeft: 24 }}
                    onClick={() => onOpen(child.id)}
                  >
                    <span className="k mono">└ {child.id}</span>
                    <span className="v" style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <Tag tone={STATE_TONE[child.state] ?? "mute"}>
                        {STATE_LABEL[child.state] ?? child.state}
                      </Tag>
                      <b>{child.title}</b>
                    </span>
                  </div>
                ))}
            </div>
          ))}
        </Card>
      )}

      {openId && (
        <Detail
          id={openId}
          onClose={() => onOpen(null)}
          onChanged={reload}
          onOpenTask={onOpenTask}
        />
      )}
    </>
  );
}

function Detail({
  id,
  onClose,
  onChanged,
  onOpenTask,
}: {
  id: string;
  onClose: () => void;
  onChanged: () => void;
  onOpenTask: (id: string) => void;
}) {
  const [detail, setDetail] = useState<RequirementDetail | null>(null);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  // 「再试一次」的草稿。预填当前文本——重试通常带着上一轮的教训,从这份文本上改。
  const [draft, setDraft] = useState("");
  const [retryOpen, setRetryOpen] = useState(false);
  const [parkReason, setParkReason] = useState("");

  const reload = useCallback(() => {
    api
      .requirement(id)
      .then((found) => {
        setDetail(found);
        setDraft(found.text);
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
  }, [id]);

  useEffect(() => {
    reload();
    return subscribe(reload);
  }, [reload]);

  if (error) return <Empty>拉不到需求详情:{error}<button className="btn" onClick={onClose}>返回需求列表</button></Empty>;
  if (!detail) return <Empty>加载中…</Empty>;

  const refresh = () => {
    reload();
    onChanged();
  };

  const retry = () => {
    api
      .submit({
        requirement: draft,
        requirement_id: detail.id,
        // 重试沿用需求当前的标题与优先级;拓扑与集测不替人预设,跟随项目缺省。
        title: detail.title,
        priority: detail.priority,
        itest: "auto",
        mode: "autonomous",
        topology: "",
      })
      .then(() => {
        setRetryOpen(false);
        setActionError("");
        refresh();
      })
      .catch((e: ApiError) => setActionError(e.detail));
  };

  const park = () => {
    api
      .patchRequirement(detail.id, { park: parkReason, resume: false })
      .then(() => {
        setParkReason("");
        setActionError("");
        refresh();
      })
      // 失败就说"没改成"并留在原状态——乐观地把界面翻成已搁置,等于伪造一个后端
      // 并不认的状态。
      .catch((e: ApiError) => setActionError(e.detail));
  };

  const resume = () => {
    api
      .patchRequirement(detail.id, { resume: true })
      .then(() => {
        setActionError("");
        refresh();
      })
      .catch((e: ApiError) => setActionError(e.detail));
  };

  const resplit = () => {
    api
      .resplitRequirement(detail.id)
      .then(() => {
        setActionError("");
        refresh();
      })
      // 边界拒绝(全部开工/没有子需求)原样展示——与 CLI 是同一份措辞。
      .catch((e: ApiError) => setActionError(e.detail));
  };

  return (
    <div style={{ marginTop: 18 }}>
      <div className="head">
        <div>
          <h1 style={{ fontSize: 18 }}>
            {detail.id} · {detail.title}
          </h1>
          <div style={{ display: "flex", gap: 7, marginTop: 8 }}>
            <Tag tone={STATE_TONE[detail.state] ?? "mute"}>
              {STATE_LABEL[detail.state] ?? detail.state}
            </Tag>
            <Tag tone="mute">{priorityLabel(detail.priority)}</Tag>
            <Tag tone="mute">累计 {detail.total_tokens.toLocaleString()} tokens</Tag>
          </div>
        </div>
        <div className="spacer" />
        <button className="btn" onClick={onClose}>
          收起
        </button>
      </div>

      {actionError && <Note tone="warn">没改成:{actionError}</Note>}
      {detail.state === "parked" && (
        <Note tone="warn">已搁置:{detail.parked}</Note>
      )}

      <Card title="当前文本">
        <pre className="log" style={{ maxHeight: 160 }}>{detail.text}</pre>
      </Card>

      {detail.children.length > 0 && (
        <Card
          title={`子需求 · ${detail.children_delivered}/${detail.children_total} 已交付`}
          extra={<span className="mono">树级 {detail.tree_tokens.toLocaleString()} tokens</span>}
        >
          {detail.children.map((child) => (
            <div key={child.id} className="kv">
              <span className="k mono">{child.id}</span>
              <span className="v" style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <Tag tone={STATE_TONE[child.state] ?? "mute"}>
                  {STATE_LABEL[child.state] ?? child.state}
                </Tag>
                {/* 停点要一眼可见:推导状态说"排队中"时,升级人工的那次尝试才是
                    要人管的地方——不标出来,它就淹没在一排"排队中"里。 */}
                {child.last_attempt_state === "ESCALATED" && <Tag tone="bad">停在人工</Tag>}
                <b>{child.title}</b>
                <span className="m">
                  {child.attempts > 0 ? `尝试 ${child.attempts} 次` : "未开工"}
                  {child.blocked_by.length > 0 && ` · 依赖 ${child.blocked_by.join("、")}`}
                  {child.parked && ` · 搁置: ${child.parked}`}
                </span>
              </span>
            </div>
          ))}
        </Card>
      )}

      <Card title={`尝试链 · ${detail.attempts} 次`}>
        {detail.chain.length === 0 && <Empty>还没有尝试</Empty>}
        {detail.chain.map((attempt, index) => (
          <div key={attempt.id} className="kv">
            <span className="k">第 {index + 1} 次</span>
            <span className="v" style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <a
                className="mono"
                href={`#/tasks/${attempt.id}`}
                onClick={(event) => {
                  event.preventDefault();
                  onOpenTask(attempt.id);
                }}
              >
                {attempt.id}
              </a>
              <Tag tone={attempt.state === "COMPLETED" ? "ok" : attempt.state === "ESCALATED" ? "bad" : "mute"}>
                {taskDisplayLabel(attempt.state, attempt.execution_status)}
              </Tag>
              {attempt.escalate_reason && <span className="m">{attempt.escalate_reason}</span>}
            </span>
          </div>
        ))}
      </Card>

      <Card title="动作">
        {!retryOpen ? (
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn pri" onClick={() => setRetryOpen(true)}>
              再试一次
            </button>
            {detail.children.length > 0 && (
              <button className="btn" onClick={resplit}>
                重新拆分剩余
              </button>
            )}
            {detail.state === "parked" ? (
              <button className="btn" onClick={resume}>
                恢复
              </button>
            ) : (
              <>
                <input
                  placeholder="搁置原因(必填)"
                  value={parkReason}
                  onChange={(e) => setParkReason(e.target.value)}
                />
                {/* 空原因禁点:搁置与恢复是相反的动作,空原因会让两者分不开。 */}
                <button className="btn" onClick={park} disabled={!parkReason.trim()}>
                  搁置
                </button>
              </>
            )}
          </div>
        ) : (
          <>
            <label>这次的需求文本 <span className="hint">· 预填当前文本,改的措辞会成为需求的最新表述</span></label>
            <textarea value={draft} onChange={(e) => setDraft(e.target.value)} />
            <div style={{ display: "flex", gap: 8 }}>
              <button className="btn pri" onClick={retry} disabled={!draft.trim()}>
                发起新尝试
              </button>
              <button className="btn" onClick={() => setRetryOpen(false)}>
                算了
              </button>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
