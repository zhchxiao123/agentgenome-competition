/**
 * 观测中心:把"进化"变成可验证的曲线。
 *
 * **样本不足时明确说数据不足,不硬画曲线。** 三个任务画出来的"下降趋势"是噪声,而一旦它被
 * 贴进汇报,后面所有判断都建立在它上面 —— 这条判断由后端的 `trends.weekly` 做出,这里只是
 * 忠实渲染 `enough` / `improving` 两个字段,不在前端加一层"看起来更好看"的插值。
 *
 * **两条柱状对比,不接 ECharts。** 五个指标、两期数值,一份轻量的条形对比就能把"在变好还是
 * 在变差"说清楚;引入一整套图表库换来的是曲线的平滑度,不是可验证性 —— 这是一处有意的简化,
 * 记在 `.scratch/INDEX.md` 的遗留账里。
 */
import { useEffect, useState } from "react";
import {
  ApiError,
  api,
  type AuditEventPage,
  type CostReport,
  type NotificationPreference,
  type TrendReport,
} from "../api/client";
import { subscribe } from "../api/live";
import { Card, Empty, Note, Tag } from "../ui/kit";
import { Activity, localToIso } from "./Activity";

const TABS = [
  ["activity", "活动流"],
  ["trends", "进化趋势"],
  ["costs", "成本看板"],
  ["audit", "审计日志"],
  ["notify", "通知偏好"],
] as const;

type ObservabilityTab = (typeof TABS)[number][0];

function isObservabilityTab(value: string | undefined): value is ObservabilityTab {
  return TABS.some(([id]) => id === value);
}

export function Observability({
  tab: requestedTab,
  onTab,
  actor = "",
}: {
  tab?: string;
  onTab?: (tab: ObservabilityTab) => void;
  actor?: string;
} = {}) {
  // 默认落在活动流:它回答的是"刚才发生了什么",而那是人打开这一页最常见的原因。
  const [localTab, setLocalTab] = useState<ObservabilityTab>("activity");
  const tab = isObservabilityTab(requestedTab) ? requestedTab : localTab;
  const selectTab = (next: ObservabilityTab) => {
    if (onTab) onTab(next);
    else setLocalTab(next);
  };

  return (
    <>
      <h1>观测中心</h1>
      <div className="sub">进化是不是真的发生了 —— 用可验证的曲线说话,而不是感觉</div>

      <div className="tabs">
        {TABS.map(([id, label]) => (
          <button key={id} className={tab === id ? "on" : ""} onClick={() => selectTab(id)}>
            {label}
          </button>
        ))}
      </div>

      {tab === "activity" && <Activity />}
      {tab === "trends" && <Trends />}
      {tab === "costs" && <Costs />}
      {tab === "audit" && <Audit />}
      {tab === "notify" && <NotifyPrefs actor={actor} />}
    </>
  );
}

// --- 趋势 ------------------------------------------------------------------

function Trends() {
  const [report, setReport] = useState<TrendReport | null>(null);
  const [windowDays, setWindowDays] = useState(7);
  const [error, setError] = useState("");

  useEffect(() => {
    setError("");
    setReport(null);
    api
      .trends(windowDays)
      .then(setReport)
      .catch((e: ApiError) => setError(e.detail));
  }, [windowDays]);

  if (error) return <Empty>拉不到趋势报告:{error}</Empty>;
  if (!report) return <Empty>加载中…</Empty>;

  return (
    <Card
      title={report.period}
      extra={
        <select value={windowDays} onChange={(e) => setWindowDays(Number(e.target.value))}>
          <option value={7}>最近 7 天 vs 前 7 天</option>
          <option value={14}>最近 14 天 vs 前 14 天</option>
          <option value={30}>最近 30 天 vs 前 30 天</option>
        </select>
      }
    >
      {!report.has_enough && (
        <Note tone="warn">
          这一期<b>数据不足,不给任何趋势结论</b>。每个指标都需要至少 10 个终态任务样本 ——
          样本不够时硬画一条曲线,后面所有判断都会建立在噪声上。
        </Note>
      )}
      <div className="grid g2" style={{ marginTop: 12 }}>
        {report.metrics.map((metric) => (
          <div className="card" key={metric.name}>
            <div className="kpi">
              <div className="lab">
                {metric.name} <span className="m">({metric.direction === "up" ? "越高越好" : "越低越好"})</span>
              </div>
              <div className="val">
                {metric.current.toFixed(2)}
                {metric.enough && (
                  <span
                    className={metric.improving === true ? "up" : metric.improving === false ? "down" : "amber"}
                    style={{ fontSize: 13, marginLeft: 8 }}
                  >
                    {metric.improving === true ? "▲ 在变好" : metric.improving === false ? "▼ 在变差" : "— 持平"}
                  </span>
                )}
              </div>
              <div className="dt">上期 {metric.previous.toFixed(2)} · 样本 {metric.samples}</div>
            </div>
            {metric.enough ? (
              <div className="bar" style={{ marginTop: 8 }}>
                <i
                  style={{
                    width: `${Math.min(100, (metric.current / Math.max(metric.current, metric.previous, 1)) * 100)}%`,
                    background: metric.improving === false ? "var(--bad)" : "var(--pri)",
                  }}
                />
              </div>
            ) : (
              <Tag tone="mute">数据不足({metric.samples} / 10)</Tag>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

// --- 成本 ------------------------------------------------------------------

function Costs() {
  const [report, setReport] = useState<CostReport | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const reload = () => api
      .costs()
      .then(setReport)
      .catch((e: ApiError) => setError(e.detail));
    reload();
    return subscribe(reload);
  }, []);

  if (error) return <Empty>拉不到成本报告:{error}</Empty>;
  if (!report) return <Empty>加载中…</Empty>;

  return (
    <div className="grid g2">
      <Card title="按员工" extra={<span className="mono">合计 {report.total_tokens.toLocaleString()} tokens</span>}>
        {report.by_employee.length === 0 ? (
          <Empty>还没有任何 token 消耗</Empty>
        ) : (
          <table>
            <thead><tr><th>员工</th><th>tokens</th></tr></thead>
            <tbody>
              {report.by_employee.map((item) => (
                <tr key={item.key}><td>{item.key}</td><td>{item.tokens.toLocaleString()}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      <Card title="成本异常任务排行" extra={<span className="m">前 20</span>}>
        {report.by_task.length === 0 ? (
          <Empty>还没有任何 token 消耗</Empty>
        ) : (
          <table>
            <thead><tr><th>任务</th><th>tokens</th></tr></thead>
            <tbody>
              {report.by_task.map((item) => (
                <tr key={item.key}><td className="mono">{item.key}</td><td>{item.tokens.toLocaleString()}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}

// --- 审计 ------------------------------------------------------------------

function Audit() {
  const [taskId, setTaskId] = useState("");
  const [actor, setActor] = useState("");
  const [kind, setKind] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [page, setPage] = useState<AuditEventPage | null>(null);
  const [error, setError] = useState("");

  const search = () => {
    api
      .auditEvents({
        task_id: taskId,
        actor,
        kind,
        ...(since ? { since: localToIso(since) } : {}),
        ...(until ? { until: localToIso(until) } : {}),
      })
      .then(setPage)
      .catch((e: ApiError) => setError(e.detail));
  };

  useEffect(search, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Card title="审计日志">
      <div className="grid g3">
        <input placeholder="任务编号" value={taskId} onChange={(e) => setTaskId(e.target.value)} />
        <input placeholder="操作人" value={actor} onChange={(e) => setActor(e.target.value)} />
        <input placeholder="事件类型,如 approval" value={kind} onChange={(e) => setKind(e.target.value)} />
      </div>
      <div className="grid g2" style={{ marginTop: 10 }}>
        <div>
          <label style={{ margin: "0 0 4px" }}>从</label>
          <input type="datetime-local" value={since} onChange={(e) => setSince(e.target.value)} />
        </div>
        <div>
          <label style={{ margin: "0 0 4px" }}>到</label>
          <input type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} />
        </div>
      </div>
      <button className="btn pri sm" style={{ marginTop: 10 }} onClick={search}>检索</button>
      {taskId && (
        <a className="btn sm" style={{ marginLeft: 8 }} href={api.auditExportUrl(taskId)}>
          导出 {taskId} 的完整审计包
        </a>
      )}
      {error && <Note tone="warn">{error}</Note>}

      {page && (
        page.items.length === 0 ? (
          <Empty>没有匹配的事件</Empty>
        ) : (
          <table style={{ marginTop: 12 }}>
            <thead><tr><th>时间</th><th>任务</th><th>操作人</th><th>类型</th></tr></thead>
            <tbody>
              {page.items.map((item, index) => (
                <tr key={index}>
                  <td className="mono">{new Date(item.ts).toLocaleString()}</td>
                  <td className="mono">{item.task_id}</td>
                  <td>{item.actor}</td>
                  <td><Tag>{item.kind}</Tag></td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      )}
    </Card>
  );
}

// --- 通知偏好 ---------------------------------------------------------------

const EVENTS = ["task_created", "cancelled", "approved", "rejected", "escalated", "completed"];

function NotifyPrefs({ actor }: { actor: string }) {
  const [events, setEvents] = useState<string[]>([]);
  const [webhook, setWebhook] = useState("");
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [configured, setConfigured] = useState(false);

  useEffect(() => {
    let current = true;
    setSaved("");
    setError("");
    if (!actor.trim()) {
      setEvents([]);
      setWebhook("");
      setConfigured(false);
      setLoading(false);
      return () => { current = false; };
    }
    setLoading(true);
    api.notificationPreferences()
      .then((page) => {
        if (!current) return;
        const preference = page.items.find((item) => item.actor === actor);
        setEvents(preference?.events ?? []);
        setWebhook(preference?.webhook_url ?? "");
        setConfigured(Boolean(preference));
        setLoading(false);
      })
      .catch((reason: ApiError) => {
        if (!current) return;
        setError(`读取失败:${reason.detail}`);
        setLoading(false);
      });
    return () => { current = false; };
  }, [actor]);

  const save = () => {
    const body: NotificationPreference = { actor, events, webhook_url: webhook || null };
    api
      .saveNotificationPreference(body)
      .then(() => {
        setConfigured(true);
        setSaved(`已保存 ${actor} 的通知偏好`);
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
  };

  return (
    <Card title="我的通知偏好">
      <Note>任务状态变化时推送到 IM。推送失败不会阻塞任何状态迁移 —— 通知只是尽力而为的增强。</Note>
      <div className="kv"><span className="k">当前身份</span><b>{actor || "尚未声明"}</b></div>
      {loading && <Empty>正在读取通知偏好…</Empty>}
      {!loading && actor && !configured && !error && <Note>{actor} 尚未配置通知偏好</Note>}
      {!actor && <Note tone="warn">先在顶栏声明身份，再编辑“我的通知偏好”。</Note>}
      <label>关心的事件</label>
      <div className="cbs">
        {EVENTS.map((event) => (
          <label key={event}>
            <input
              type="checkbox"
              disabled={!actor || loading || Boolean(error)}
              checked={events.includes(event)}
              onChange={(e) =>
                setEvents((current) => (e.target.checked ? [...current, event] : current.filter((x) => x !== event)))
              }
            />
            {event}
          </label>
        ))}
      </div>
      <label>IM Webhook 地址</label>
      <input value={webhook} disabled={!actor || loading || Boolean(error)} onChange={(e) => setWebhook(e.target.value)} placeholder="https://open.feishu.cn/..." />
      {error && <div className="err on">{error}</div>}
      {saved && <Note>{saved}</Note>}
      <button className="btn pri" style={{ marginTop: 10 }} onClick={save} disabled={!actor.trim() || loading || Boolean(error)}>
        保存
      </button>
    </Card>
  );
}
