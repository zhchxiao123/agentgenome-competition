import { useState } from "react";
import { ApiError, api, type WorkerPlanView } from "../api/client";
import { Card, Note } from "../ui/kit";

/**
 * "这次点下去会发生什么",以及点下去。
 *
 * ## 为什么值一个按钮而不是随页面自动加载
 *
 * 算计划要把整份花名册在平台上读一遍,那是一串真实的网络往返。让它随「员工管理」这一页
 * 一起加载,等于每次打开这一页都去敲一遍平台——而绝大多数打开与供应无关。
 *
 * ## 为什么三态要分开说
 *
 * "将对齐"对管理员没有信息量。**新建会真的拉起容器并花掉一次模型探活,更新不会**——
 * 他要决定的正是这一点。第四态 `unknown` 也不能混进"无变化":平台没答上来时报"无变化"
 * 是在替平台撒谎。
 *
 * ## 执行入口只在看过计划之后出现
 *
 * 这不是安全边界(服务端仍然要 `edit_settings`),是**预期边界**:每建一个 Worker 都会
 * 真的拉起容器并触发一次模型探活。让人先看见后果再点,与删除要二次确认是同一件事。
 *
 * ## 逐个供应,不是一次提交一整份
 *
 * 供应是长动作。逐个走,进度就是**真的**(第几个做完了),一个失败也天然不拖垮其余——
 * 不需要在服务端再造一套"部分成功"的语义。
 */

/** 动作 → 那句话说的后果。**一份数据**,不散成一串三元表达式。 */
const ACTIONS: Record<string, string> = {
  created: "新建 · 会拉起一个容器并做一次模型探活",
  updated: "更新 · 换掉平台上的身份文件与模型,不换房间",
  unchanged: "无变化 · 一个字节都不会写",
  unknown: "读不到 · 平台没答上来",
};

/** 执行结果里那几个动作的中文。汇总只说人关心的三态。 */
const DONE_LABELS: [string, string][] = [
  ["created", "新建"],
  ["updated", "更新"],
  ["unchanged", "无变化"],
];

type Progress = { at: number; total: number; employee: string };

export function WorkerPlanCard({ onProvisioned }: { onProvisioned?: () => void } = {}) {
  const [plan, setPlan] = useState<WorkerPlanView | null>(null);
  const [failed, setFailed] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [done, setDone] = useState<string[]>([]);
  const [errors, setErrors] = useState<{ employee: string; detail: string }[]>([]);

  const look = () => {
    setBusy(true);
    setFailed("");
    setDone([]);
    setErrors([]);
    // 每次都重算。**不缓存**:计划描述的是平台此刻的样子,而平台会在两次点击之间变。
    api
      .workerPlan()
      .then(setPlan)
      .catch((e: ApiError) => {
        setFailed(e.detail);
        setPlan(null);
      })
      .finally(() => setBusy(false));
  };

  const run = async () => {
    if (!plan) return;
    setBusy(true);
    setFailed("");
    setDone([]);
    setErrors([]);
    const actions: string[] = [];
    const wrong: { employee: string; detail: string }[] = [];
    for (const [index, row] of plan.items.entries()) {
      setProgress({ at: index + 1, total: plan.items.length, employee: row.employee_id });
      try {
        const result = await api.provisionWorker(row.employee_id);
        actions.push(result.action);
      } catch (e) {
        // **一个失败不拖垮其余。** 一次小故障不该让整份花名册停在半路。
        wrong.push({ employee: row.employee_id, detail: (e as ApiError).detail });
      }
    }
    setProgress(null);
    setDone(actions);
    setErrors(wrong);
    setBusy(false);
    // 状态列要跟着变,否则供应完了人看到的还是"未供应"。**父组件重读**,不在这里
    // 乐观合并——平台可能因为别的原因落到别的状态上。
    onProvisioned?.();
  };

  const summary = DONE_LABELS.map(([action, label]) => {
    const count = done.filter((one) => one === action).length;
    return count > 0 ? `${count} 个${label}` : "";
  }).filter(Boolean);

  return (
    <Card title="供应计划" extra={<span className="m">只读预演</span>}>
      <button className="btn" disabled={busy} onClick={look}>
        {busy && !progress ? "读平台中…" : "查看计划"}
      </button>

      {failed && <Note tone="warn">算不出计划:{failed}</Note>}

      {plan && plan.items.length === 0 && (
        <Note>这个工作区还没有跑在容器运行时上的员工——没有要对齐的东西。</Note>
      )}

      {plan && plan.items.length > 0 && (
        <>
          {!plan.can_provision && <Note tone="warn">你没有供应权限,这份计划只能看。</Note>}
          <table>
            <thead>
              <tr>
                <th>员工</th>
                <th>这次会做什么</th>
              </tr>
            </thead>
            <tbody>
              {plan.items.map((row) => (
                <tr key={row.employee_id}>
                  <td className="mono">{row.employee_id}</td>
                  <td className={row.action === "unknown" ? "warn" : ""}>
                    {ACTIONS[row.action] ?? row.action}
                    {row.detail && <div className="m">{row.detail}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <button
            className="btn"
            style={{ marginTop: 12 }}
            disabled={!plan.can_provision || busy}
            onClick={run}
          >
            执行供应
          </button>

          {/* 转圈只说"还在动",这里要说清**动到哪儿了**——一个员工实测约十秒。 */}
          {progress && (
            <Note>
              正在供应 {progress.at} / {progress.total}:
              <span className="mono">{progress.employee}</span>
            </Note>
          )}

          {summary.length > 0 && <Note>供应完成:{summary.join(",")}。</Note>}

          {errors.length > 0 && (
            <Note tone="warn">
              这些没成,其余照常完成了:
              <ul>
                {errors.map((one) => (
                  <li key={one.employee}>
                    <span className="mono">{one.employee}</span>:{one.detail}
                  </li>
                ))}
              </ul>
            </Note>
          )}
        </>
      )}
    </Card>
  );
}
