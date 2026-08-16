import { useState } from "react";
import { ApiError, api } from "../api/client";

/**
 * 回收一个员工的容器:休眠或删除。
 *
 * ## 两个动作的确认强度不一样,是刻意的
 *
 * **休眠可逆**——休眠的 Worker 在下一次派发时自动唤醒(PRD 32 已实现),所以它是纯粹的
 * 成本动作,不会变成"任务莫名不动了"。给它加确认,只会训练人闭着眼点确认,而删除那一个
 * 真的需要他看清。
 *
 * **删除不可逆**:重建会换掉房间,而房间 id 是不落盘的(真机实测过)。所以这里问一遍,
 * 服务端还要 `confirm=true` 再问一遍——弹窗不是边界,一条手滑的 curl 一样能删。
 *
 * ## 未供应的员工没有这一格
 *
 * 没有容器可回收时给一对灰按钮,等于让人对着两个永远点不出结果的东西猜为什么。
 */
export function WorkerRecycleActions({
  employee,
  status,
  canEdit,
  onChanged,
}: {
  employee: string;
  /** 这个员工此刻的容器状态。`absent` / `unknown` 时没有可回收的东西。 */
  status: string;
  canEdit: boolean;
  /** 做完了。**父组件重读状态**,不在这里乐观合并——平台可能落到别的状态上。 */
  onChanged: () => void;
}) {
  const [asking, setAsking] = useState(false);
  const [failed, setFailed] = useState("");
  const [busy, setBusy] = useState(false);

  if (status !== "running" && status !== "sleeping") return null;

  const act = (call: () => Promise<unknown>) => {
    setBusy(true);
    setFailed("");
    call()
      .then(() => {
        setAsking(false);
        onChanged();
      })
      .catch((e: ApiError) => setFailed(e.detail))
      .finally(() => setBusy(false));
  };

  return (
    <div className="worker-actions">
      {status === "running" && (
        <button
          className="btn sm"
          disabled={!canEdit || busy}
          onClick={() => act(() => api.sleepWorker(employee))}
        >
          休眠
        </button>
      )}
      <button className="btn sm" disabled={!canEdit || busy} onClick={() => setAsking(true)}>
        删除
      </button>

      {asking && (
        <div className="confirm">
          {/* 说清后果,而不是"确定吗?"——后者不给人任何做判断的材料。 */}
          <p>删除不可逆:容器会被销毁,重建时房间 id 会变,原来那个房间找不回来。</p>
          <button
            className="btn sm"
            disabled={!canEdit || busy}
            onClick={() => act(() => api.deleteWorker(employee))}
          >
            确认删除
          </button>
          <button className="btn sm" disabled={busy} onClick={() => setAsking(false)}>
            取消
          </button>
        </div>
      )}

      {failed && <div className="err on">{failed}</div>}
    </div>
  );
}
