/**
 * 会话的前端状态机。
 *
 * ```
 * idle ──send──▶ sending ──first-block──▶ streaming ⇄ tool-running ──end──▶ done
 *                    └──error──▶ error ──retry──▶ sending
 *   streaming/tool-running ──stop──▶ done(真的打断了后端那一轮,不只是本地不看了)
 * ```
 *
 * ## `tool-running` 为什么单列
 *
 * 它与 `streaming` 的**等待体感不同**:前者要显示"正在读 xxx"这样的具体动作,后者只要
 * 光标在动。合成一个状态的话,工具调用期间界面看起来像卡住了——而工具调用恰恰是耗时
 * 最长的那一段。
 *
 * ## 断线补齐不自己发明协议
 *
 * 记住最后收到的块序号,重连后走 `?after=<seq>` 拉补齐段。**后端的持久日志是事实源,
 * 前端只是追它**——自己维护一套增量协议是分布式系统里最容易出错的一类代码。
 *
 * 一轮问答本身跑在后端的一个后台任务里,不绑定任何一次连接:关掉页面不会打断它,
 * `reload()` 发现 `session.generating` 为真就用同一条补齐协议接上直播,而不是傻等一条
 * 永远不会来的块。真正的"打断"因此必须是一次显式的后端调用(`stop`),不能再靠断连接
 * 顺带实现。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, type BlockItem, type SessionSummary } from "../api/client";

export type ChatState = "idle" | "sending" | "streaming" | "tool-running" | "done" | "error";

export type UseSession = {
  session: SessionSummary | null;
  blocks: BlockItem[];
  state: ChatState;
  error: string;
  send: (message: string) => Promise<void>;
  retry: () => Promise<void>;
  stop: () => void;
  reload: () => void;
};

/** 最后一块的序号。补齐从这里续。 */
const lastSeq = (blocks: BlockItem[]): number =>
  blocks.reduce((max, block) => Math.max(max, block.seq), 0);

/** 按 kind 换算成等待态:工具块要显示具体在读什么,其余块只要光标在动。 */
const stateFor = (kind: string): ChatState => (kind === "tool-step" ? "tool-running" : "streaming");

export function useSession(sessionId: string | null): UseSession {
  const [session, setSession] = useState<SessionSummary | null>(null);
  const [blocks, setBlocks] = useState<BlockItem[]>([]);
  const [state, setState] = useState<ChatState>("idle");
  const [error, setError] = useState("");
  // 重试要用它:出错时用户按的是"再试一次",而不是重新打一遍。
  const lastMessage = useRef("");
  const stopped = useRef(false);
  // 接直播用的连接。**每次重挂都要先断掉上一个**——否则切会话或者重新 reload 时会有
  // 两条直播同时在写同一份 state,谁先谁后完全看运气。
  const tail = useRef<AbortController | null>(null);

  const appendBlock = useCallback((block: BlockItem) => {
    if (stopped.current) return;
    setBlocks((current) => (current.some((b) => b.seq === block.seq) ? current : [...current, block]));
    setState(stateFor(block.kind));
  }, []);

  const attach = useCallback(
    (id: string, after: number) => {
      tail.current?.abort();
      const controller = new AbortController();
      tail.current = controller;
      setState((current) => (current === "idle" || current === "done" ? "streaming" : current));
      api
        .attachSessionStream(id, after, appendBlock, controller.signal)
        .then(() => {
          if (controller.signal.aborted) return;
          setState("done");
          api.session(id).then(setSession).catch(() => undefined);
        })
        .catch((thrown) => {
          if (controller.signal.aborted) return; // 我们自己断的,不是错误
          setError(thrown instanceof ApiError ? thrown.detail : String(thrown));
          setState("error");
        });
    },
    [appendBlock],
  );

  const reload = useCallback(() => {
    if (!sessionId) return;
    tail.current?.abort();
    // 两次拉取各自独立、各自兜底:会话摘要拿不到不该连历史都不显示,反过来也一样。
    // 但"要不要接直播"需要两边都到齐才能判断,用两个局部标记做个简单的会合。
    let generating = false;
    let historyReady = false;
    let historyLastSeq = 0;
    const maybeAttach = () => {
      if (generating && historyReady) attach(sessionId, historyLastSeq);
    };

    api
      .session(sessionId)
      .then((summary) => {
        setSession(summary);
        generating = summary.generating ?? false;
        maybeAttach();
      })
      .catch(() => undefined);
    api
      .sessionMessages(sessionId)
      .then((page) => {
        setBlocks(page.items);
        historyReady = true;
        historyLastSeq = lastSeq(page.items);
        maybeAttach();
      })
      .catch((e: ApiError) => setError(e.detail));
  }, [sessionId, attach]);

  useEffect(reload, [reload]);
  // 组件卸载时把还接着的直播断掉——纯粹是清理我们自己的订阅位,后端那一轮不受影响。
  useEffect(() => () => tail.current?.abort(), []);

  const run = useCallback(
    async (message: string) => {
      if (!sessionId) return;
      stopped.current = false;
      lastMessage.current = message;
      setError("");
      setState("sending");
      // 乐观更新:界面不该在网络往返期间看起来卡住。第一块权威回声一到就会把它换掉。
      const optimisticSeq = lastSeq(blocks) + 1;
      const optimistic: BlockItem = {
        seq: optimisticSeq,
        kind: "text",
        text: message,
        detail: { role: "user" },
      } as BlockItem;
      setBlocks((current) => [...current, optimistic]);
      // **乐观块只摘一次。** 权威回声的 seq 与它**必然相同**——前端的 `lastSeq(blocks)` 与
      // 后端的 `session.last_seq` 恒等,而后端给用户那条编的号就是 `last_seq + 1`。所以
      // "每收到一块就按 seq 摘一次"会在第二块到达时摘掉刚落位的权威回声:用户自己那句
      // 在员工开始输出内容的瞬间消失,而刷新页面又会回来(补齐走的是历史,不经这条路),
      // 这个"时有时无"正是它极难归因的原因。
      let optimisticSettled = false;

      try {
        await api.sendSessionMessage(sessionId, message, (block) => {
          if (stopped.current) return;
          // 标记在更新函数**外面**置位:更新函数必须是纯的(StrictMode 下会跑两遍)。
          const dropOptimistic = !optimisticSettled;
          optimisticSettled = true;
          setBlocks((current) => {
            const base = dropOptimistic
              ? current.filter((b) => b.seq !== optimisticSeq)
              : current;
            return base.some((b) => b.seq === block.seq) ? base : [...base, block];
          });
          setState(stateFor(block.kind));
        });
        if (stopped.current) return;
        setState("done");
        api.session(sessionId).then(setSession).catch(() => undefined);
      } catch (thrown) {
        if (stopped.current) return;
        setError(thrown instanceof ApiError ? thrown.detail : String(thrown));
        setState("error");
      }
    },
    [sessionId, blocks],
  );

  const send = useCallback((message: string) => run(message), [run]);
  const retry = useCallback(() => run(lastMessage.current), [run]);
  const stop = useCallback(() => {
    stopped.current = true;
    tail.current?.abort();
    setState("done");
    // 打断是真的打后端那一轮,不再是"断开连接顺带实现"——发出去就行,界面已经本地
    // 更新过了,不用等它回来。
    if (sessionId) api.stopSession(sessionId).catch(() => undefined);
  }, [sessionId]);

  return { session, blocks, state, error, send, retry, stop, reload };
}
