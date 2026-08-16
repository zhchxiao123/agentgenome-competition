/**
 * 服务端推送。
 *
 * ## 它只做一件事:告诉页面「有变化了」
 *
 * 回调里**不带业务数据**,页面收到之后自己去重新拉。做成数据通道的话,断线那一刻页面就
 * 永久停在旧状态,而"重连补齐"会变成一套需要自己维护正确性的增量协议——那是分布式系统里
 * 最容易出错的一类代码。
 *
 * 现在这个形态下,推送挂了最坏结果只是**更新慢一点**,而且不需要任何额外代码去保证。
 */
import { getWorkspace } from "./client";

export type Notice = { task_id: string; kind: string };

export function subscribe(onChange: (notice: Notice) => void, taskId?: string): () => void {
  // 订阅与拉取带同一个项目参数(EventSource 加不了请求头,走 query)。多项目实例上
  // 服务端对"没说要哪个项目"的订阅默认拒绝——task_id 本身就是数据,不该跨项目可见。
  const params = new URLSearchParams();
  if (taskId) params.set("task_id", taskId);
  const workspace = getWorkspace();
  if (workspace) params.set("workspace", workspace);
  const query = params.size ? `?${params}` : "";
  const source = new EventSource(`${import.meta.env.VITE_API_BASE ?? ""}/events/stream${query}`);

  // `EventSource` 自带重连并会带上 Last-Event-ID,后端据此补发。**补发失败也不致命**——
  // 页面的拉取策略本来就能自愈,这是同一个设计决定的两面。
  source.onmessage = (event) => {
    try {
      onChange(JSON.parse(event.data) as Notice);
    } catch {
      // 解析不了的推送直接丢。它顶多让这一次刷新晚一点,而抛出去会把整条订阅断掉。
    }
  };
  return () => source.close();
}
