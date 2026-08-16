import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Note } from "../ui/kit";

/**
 * 项目建好之后的那一步:配了容器运行时,但一个员工都还没被供应。
 *
 * ## 为什么值一条提示
 *
 * 供应是**显式动作**,不在派发路径上自动发生(建一个 Worker 会真的拉起容器并花掉一次
 * 模型探活)。这个设计的代价是:配完运行时之后,界面上什么都不会自己发生——人对着一个
 * 空的状态列,没有任何东西告诉他还差一步。
 *
 * ## 三个"不出现"同样重要
 *
 * - **没配容器运行时不出现**:对着一个没启用的能力催人下一步是纯噪声。
 * - **已经供应过不出现**:一条永远在的提示会被当成背景色,于是它真正要紧的那次也没人看。
 * - **读不到配置不出现**:拿一句猜出来的提示顶上,比不提示更糟。
 */

const AGENTTEAMS = "agentteams";

export function ContainerOnboarding() {
  const [show, setShow] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .settings()
      .then((settings) => {
        if (!(AGENTTEAMS in (settings.runtime?.runtimes ?? {}))) return null;
        // 配了才去问状态:没配的话这一问必然 400,而那个 400 不是信息。
        return api.workerStatuses();
      })
      .then((view) => {
        if (!alive || !view) return;
        const live = view.items.some(
          // 休眠也算供应过——它只是省钱,不是没建。
          (item) => item.status === "running" || item.status === "sleeping",
        );
        setShow(!live);
      })
      .catch(() => setShow(false));
    return () => {
      alive = false;
    };
  }, []);

  if (!show) return null;

  return (
    <Note>
      这个项目配了容器运行时,但还没有员工被供应到平台上。去<a href="#/roster">员工管理</a>
      看一遍供应计划,确认之后一键对齐——供应是显式动作,派发时不会自己发生。
    </Note>
  );
}
