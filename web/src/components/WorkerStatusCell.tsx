import type { WorkerStatusView } from "../api/client";

/**
 * 一个员工在容器平台上的当下事实,一格。
 *
 * ## 四态,不是三态
 *
 * `unknown`(平台没答上来)与 `absent`(确实没供应过)读起来必须不一样。合成一个的话,
 * 平台一挂,整份花名册看起来就像从没供应过——而那会诱人去点一次本不必要的供应,那次
 * 供应会真的拉起容器并花掉一次模型探活。
 *
 * ## 本地员工是空格,不是「未供应」
 *
 * 跑在本地运行时的员工没有 Worker 可言。给它写「未供应」等于告诉人"这里缺了点什么、
 * 去补上"——而其实什么都不缺。
 */

/** 状态 → 给人看的那句话。**一份数据**,别散成一串三元表达式。 */
const LABELS: Record<string, string> = {
  running: "运行中",
  sleeping: "休眠中",
  absent: "未供应",
  unknown: "读不到",
};

export function WorkerStatusCell({ status }: { status?: WorkerStatusView }) {
  if (!status) return null;

  const label = LABELS[status.status] ?? status.status;
  const failed = status.status === "unknown";

  return (
    <div className={`worker-status ${status.status}`}>
      <span className={failed ? "warn" : ""}>{label}</span>
      {/* 房间与 Worker 名是排障时唯一要往平台上贴的两个串,所以用等宽显示全量、不截断。 */}
      {status.worker && <div className="m mono">{status.worker}</div>}
      {status.room && <div className="m mono">{status.room}</div>}
      {failed && status.detail && <div className="m">{status.detail}</div>}
    </div>
  );
}
