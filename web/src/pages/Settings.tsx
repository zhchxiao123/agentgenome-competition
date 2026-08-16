/** 项目级运行参数的统一管理页。每张卡只保存一个顶层配置段，避免并发编辑互相覆盖。 */
import { useEffect, useState } from "react";
import { ApiError, api, type SettingsView } from "../api/client";
import { Card, Empty, Note } from "../ui/kit";

type Section =
  | "runtime"
  | "budgets"
  | "limits"
  | "concurrency"
  | "itest"
  | "genome_tasks"
  | "topology"
  | "approval";

export function Settings() {
  const [settings, setSettings] = useState<SettingsView | null>(null);
  const [loadError, setLoadError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState<Section | "">("");
  const [saved, setSaved] = useState<Section | "">("");

  useEffect(() => {
    api.settings().then(setSettings).catch((e: ApiError) => setLoadError(e.detail));
  }, []);

  if (loadError) return <Empty>读不到系统设置：{loadError}</Empty>;
  if (!settings) return <Empty>加载中…</Empty>;

  const editable = settings.can_edit;
  const critique = settings.topology.critique;
  const bestOfN = settings.topology.best_of_n;
  const patch = <K extends Section>(section: K, value: SettingsView[K]) => {
    setSettings({ ...settings, [section]: value });
    setSaved("");
    setSaveError("");
  };
  const save = (section: Section, label: string) => {
    setSaving(section);
    setSaved("");
    setSaveError("");
    api.updateSettings(section, settings[section])
      .then(() => setSaved(section))
      .catch((e: ApiError) => setSaveError(`${label}没保存：${e.detail}`))
      .finally(() => setSaving(""));
  };

  const number = (
    label: string,
    value: number,
    onChange: (value: number) => void,
    min = 1,
  ) => (
    <div>
      <label htmlFor={label}>{label}</label>
      <input
        id={label}
        type="number"
        min={min}
        value={value}
        disabled={!editable}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </div>
  );
  const button = (section: Section, label: string) => (
    <button
      className="btn"
      style={{ marginTop: 12 }}
      disabled={!editable || saving === section}
      onClick={() => save(section, label)}
    >
      {saving === section ? "保存中…" : `保存 ${label}`}
    </button>
  );

  return (
    <>
      <h1>系统设置</h1>
      <div className="sub">统一管理当前项目的额度、轮次、超时与执行容量</div>
      {!editable && <Note tone="warn">你没有改系统设置的权限。</Note>}
      {saveError && <div className="err on">{saveError}</div>}
      {saved && <Note>已保存；后续新任务和新会话会使用这组配置。</Note>}

      <Card title="运行时轮次" extra={<span className="m">runtime</span>}>
        <div className="grid g2" style={{ gap: 14 }}>
          {Object.entries(settings.runtime.runtimes ?? {}).map(([name, runtime]) =>
            number(`${name} 最大轮数`, runtime.max_turns, (value) => patch("runtime", {
              ...settings.runtime,
              runtimes: {
                ...settings.runtime.runtimes,
                [name]: { ...runtime, max_turns: value },
              },
            })),
          )}
        </div>
        {button("runtime", "运行时轮次")}
      </Card>

      <Card title="Token 预算" extra={<span className="m">budgets</span>}>
        <label htmlFor="enforce-budget">强制执行任务与作业预算</label>
        <select
          id="enforce-budget"
          value={settings.budgets.enforce ? "on" : "off"}
          disabled={!editable}
          onChange={(event) => patch("budgets", {
            ...settings.budgets,
            enforce: event.target.value === "on",
          })}
        >
          <option value="off">off · 记录用量但不因预算中止</option>
          <option value="on">on · 达到额度时中止</option>
        </select>
        <div className="grid g2" style={{ gap: 14, marginTop: 12 }}>
          {number("单任务 token 上限", settings.budgets.per_task_tokens, (value) =>
            patch("budgets", { ...settings.budgets, per_task_tokens: value }))}
          {number("单作业 token 上限", settings.budgets.per_job_tokens, (value) =>
            patch("budgets", { ...settings.budgets, per_job_tokens: value }))}
          {number("单会话 token 上限", settings.budgets.session_tokens, (value) =>
            patch("budgets", { ...settings.budgets, session_tokens: value }), 0)}
        </div>
        {button("budgets", "Token 预算")}
      </Card>

      <Card title="执行限制" extra={<span className="m">limits</span>}>
        <div className="grid g2" style={{ gap: 14 }}>
          {number("修复轮次上限", settings.limits.max_fix_rounds, (value) =>
            patch("limits", { ...settings.limits, max_fix_rounds: value }))}
          {number("单作业超时（秒）", settings.limits.job_timeout_s, (value) =>
            patch("limits", { ...settings.limits, job_timeout_s: value }))}
          {number("计划外扩权次数", settings.limits.max_scope_grants, (value) =>
            patch("limits", { ...settings.limits, max_scope_grants: value }))}
          {number("需求树最大深度", settings.limits.split_max_depth, (value) =>
            patch("limits", { ...settings.limits, split_max_depth: value }))}
          {number("单次拆分子需求数", settings.limits.split_max_children, (value) =>
            patch("limits", { ...settings.limits, split_max_children: value }))}
        </div>
        {button("limits", "执行限制")}
      </Card>

      <Card title="执行容量" extra={<span className="m">concurrency</span>}>
        <div className="grid g2" style={{ gap: 14 }}>
          {number("全局并发作业数", settings.concurrency.global_jobs, (value) =>
            patch("concurrency", { ...settings.concurrency, global_jobs: value }))}
        </div>
        {button("concurrency", "执行容量")}
      </Card>

      <Card title="基因组任务" extra={<span className="m">genome_tasks</span>}>
        <div className="grid g2" style={{ gap: 14 }}>
          {number("基因组任务 token 上限", settings.genome_tasks.per_task_tokens, (value) =>
            patch("genome_tasks", { ...settings.genome_tasks, per_task_tokens: value }))}
          {number("基因组任务并发数", settings.genome_tasks.concurrent_jobs, (value) =>
            patch("genome_tasks", { ...settings.genome_tasks, concurrent_jobs: value }))}
          {number("边界确认提醒（小时）", settings.genome_tasks.confirmation_reminder_hours, (value) =>
            patch("genome_tasks", {
              ...settings.genome_tasks,
              confirmation_reminder_hours: value,
            }))}
        </div>
        {button("genome_tasks", "基因组任务")}
      </Card>

      {critique && bestOfN && <Card title="拓扑轮次" extra={<span className="m">topology</span>}>
        <div className="grid g2" style={{ gap: 14 }}>
          {number("评审精化轮次上限", critique.max_rounds, (value) =>
            patch("topology", {
              ...settings.topology,
              critique: { ...critique, max_rounds: value },
            }))}
          {number("Best-of-N 尝试上限", bestOfN.max_attempts, (value) =>
            patch("topology", {
              ...settings.topology,
              best_of_n: { ...bestOfN, max_attempts: value },
            }))}
        </div>
        {button("topology", "拓扑轮次")}
      </Card>}

      <Card title="集成测试" extra={<span className="m">itest</span>}>
        <div className="grid g2" style={{ gap: 14 }}>
          <div>
            <label htmlFor="compose-file">Compose 文件</label>
            <input id="compose-file" value={settings.itest.compose_file} disabled={!editable}
              onChange={(event) => patch("itest", { ...settings.itest, compose_file: event.target.value })} />
          </div>
          {number("集成测试超时（秒）", settings.itest.timeout_s, (value) =>
            patch("itest", { ...settings.itest, timeout_s: value }))}
          {number("失败日志尾部行数", settings.itest.log_tail_lines, (value) =>
            patch("itest", { ...settings.itest, log_tail_lines: value }))}
          <div>
            <label htmlFor="seed-service">灌数服务</label>
            <input id="seed-service" value={settings.itest.seed_service} disabled={!editable}
              onChange={(event) => patch("itest", { ...settings.itest, seed_service: event.target.value })} />
          </div>
          <div>
            <label htmlFor="seed-command">灌数命令</label>
            <input id="seed-command" value={settings.itest.seed_cmd} disabled={!editable}
              onChange={(event) => patch("itest", { ...settings.itest, seed_cmd: event.target.value })} />
          </div>
        </div>
        {button("itest", "集成测试")}
      </Card>

      <Card title="审批与通知" extra={<span className="m">approval</span>}>
        <label htmlFor="approvers">审批人（每行一个）</label>
        <textarea id="approvers" rows={4} value={(settings.approval.approvers ?? []).join("\n")}
          disabled={!editable} onChange={(event) => patch("approval", {
            ...settings.approval,
            approvers: event.target.value.split("\n").map((item) => item.trim()).filter(Boolean),
          })} />
        <label htmlFor="webhook">通知 Webhook</label>
        <input id="webhook" value={settings.approval.notify?.webhook ?? ""} disabled={!editable}
          onChange={(event) => patch("approval", {
            ...settings.approval,
            notify: { webhook: event.target.value || null },
          })} />
        {button("approval", "审批与通知")}
      </Card>
    </>
  );
}
