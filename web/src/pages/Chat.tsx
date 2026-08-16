/**
 * 对话工作台:三栏 —— 会话列表 / 对话流 / 证据面板。
 *
 * 交互原型见 `docs/design/console-p0.html` 的「对话工作台」页。
 *
 * ## 模式是静态标签,不是 Tab
 *
 * 后端 `mode` 不可变(没有任何 API 能改),前端就**不能画成可点控件**——那类控件在暗示
 * "随时可切",而咨询只读、结对可写,切换意味着一次没有闸门的提权。会话列表上方按
 * **状态**筛选而不是模式,避免两个互相矛盾的控件。
 *
 * ## 上下文条是输入面,证据面板是输出面
 *
 * 两者内容高度重叠(都是卡片/文件/产物),必须把职责差别做出来,否则用户会以为同一份
 * 东西显示了两遍。上下文条在**对话栏内部**——它是这个会话的属性,不是页面的。
 */
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { ApiError, api, type BlockItem, type SessionSummary, type TaskDraft } from "../api/client";
import { NewSessionDialog } from "../chat/NewSessionDialog";
import { BlockRun, BlockView, groupBlocks } from "../chat/blocks";
import { useSession } from "../chat/useSession";
import { Card, Empty, Note, Tag, type Tone, stateTone } from "../ui/kit";

/**
 * 读写权限 → 标签。**一份映射**,列表与标题共用。
 *
 * 用户真正在读的一直就是"可写 / 只读"这两个字——早先还在它前面多摆一个模式名标签
 * (咨询/质询/结对),那是一次多余的翻译:三个模式名回答的问题,最后还是要靠旁边那个
 * "可写/只读"来回答。
 */
function permissionTag(writable: boolean): { tone: Tone; label: string } {
  return writable ? { tone: "warn", label: "可写" } : { tone: "pri", label: "只读" };
}
const STATE_LABEL: Record<string, string> = {
  active: "进行中",
  suspended: "已挂起",
  ended: "已结束",
};
/** 挂起原因。只显示「已挂起」不说为什么,用户的第一反应是「它坏了」。 */
const SUSPEND_LABEL: Record<string, string> = {
  idle: "空闲超时自动挂起",
  budget: "token 预算耗尽",
};

/** 列表里只给**时分**。日期在长列表里是噪声——同一天的会话占绝大多数。 */
function clockOf(stamp: string): string {
  const at = new Date(stamp);
  if (Number.isNaN(at.getTime())) return "";
  return `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
}

/** `12400` → `12.4k`。原样打印的话,用户要自己数位数才知道是一万二还是十二万。 */
function shortTokens(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(1).replace(/\.0$/, "")}k` : String(value);
}

/**
 * 这个上限是不是"不限"。**0 = 不限,不是"上限是 0"。**
 *
 * 判定与后端 `sessions.model.unlimited` 同义;两边各写一遍是因为这一层拿到的只是一个数字,
 * 而把它误读成"上限是 0"的后果在两边完全不同:后端是第一轮就判超预算,前端是进度条恒为
 * 满格并且写着"已超支"。
 */
function unlimitedBudget(maxTokens: number): boolean {
  return !maxTokens || maxTokens <= 0;
}

/** 预算用掉的百分比。**封顶 100**——超支时进度条不该溢出容器。 */
function budgetPercent(session: { tokens_used: number; max_tokens: number }): number {
  if (unlimitedBudget(session.max_tokens)) return 0;
  return Math.min(100, Math.round((session.tokens_used / session.max_tokens) * 100));
}

const STATE_FILTERS = [
  ["", "全部"],
  ["active", "进行中"],
  ["suspended", "已挂起"],
  ["ended", "已结束"],
] as const;

export function Chat({ onOpenTask }: { onOpenTask?: (taskId: string) => void }) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [total, setTotal] = useState(0);
  const [picked, setPicked] = useState<string | null>(null);
  const [stateFilter, setStateFilter] = useState<string>("");
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);

  const reload = () => {
    api
      .sessions(stateFilter ? { state: stateFilter } : {})
      .then((found) => {
        setSessions(found.items);
        setCounts(found.counts ?? {});
        setTotal(found.total ?? found.items.length);
        setError("");
        setPicked((current) => current ?? found.items[0]?.id ?? null);
      })
      // 拉不到与「一个会话都没有」在空列表上长得一样,而前者要立刻修。
      .catch((e: ApiError) => setError(e.detail));
  };

  useEffect(reload, [stateFilter]); // eslint-disable-line react-hooks/exhaustive-deps

  // 原来独立的"↺ 会话回放"按钮已经拿掉,行为并进"已结束"这档筛选——**回放 = 进入已结束
  // 会话的只读视图**,而"已结束"chip 本来就是同一个筛选条件。多的这一步是清空 `picked`,
  // 不然切完档中栏还停在一条已经不在筛选结果里的会话上,像是筛选没生效。
  const onFilterChange = (value: string) => {
    if (value === "ended") setPicked(null);
    setStateFilter(value);
  };

  return (
    // 页头(标题/副标题/按钮)拿掉之后 `.chat` 三栏从 `.main` 顶部就开始——`.chat-page`
    // 撑满 `.main` 分给这一页的高度,`.chat{flex:1}` 吃掉全部,不用猜一个 `calc(100vh - 常数)`。
    <div className="chat-page">
      {error && <Note tone="warn">拉不到会话列表:{error}</Note>}

      <div className="chat">
        <SessionList
          sessions={sessions}
          picked={picked}
          onPick={setPicked}
          stateFilter={stateFilter}
          onFilter={onFilterChange}
          counts={counts}
          total={total}
          onCreate={() => setCreating(true)}
        />
        {picked ? (
          <Conversation sessionId={picked} onChanged={reload} onOpenTask={onOpenTask} />
        ) : (
          <Card title="对话">
            {/* 这句话以前没有对应的可点控件——**界面不该承诺一个不存在的动作**。 */}
            <Empty>
              还没有会话 —— <a onClick={() => setCreating(true)}>新建一个开始问</a>
            </Empty>
          </Card>
        )}
        {picked ? <Evidence session={sessions.find((s) => s.id === picked) ?? null} /> : <div />}
      </div>

      {/* **覆盖层,不占中栏。** 早先它顶掉对话栏、还连带把证据面板一起藏了——新建时整页
          塌掉一半,而用户只是想开个会话。对话框该盖在页面上,不该改页面的骨架。 */}
      {creating && (
        <NewSessionDialog
          onCreated={(id) => {
            setCreating(false);
            // 建完直接进这个会话——建完还要自己去列表里找它,等于把一次成功当成半次。
            setPicked(id);
            reload();
          }}
          onClose={() => setCreating(false)}
        />
      )}
    </div>
  );
}

// --- 会话列表 -----------------------------------------------------------------

function SessionList({
  sessions,
  picked,
  onPick,
  stateFilter,
  onFilter,
  counts,
  total,
  onCreate,
}: {
  sessions: SessionSummary[];
  picked: string | null;
  onPick: (id: string) => void;
  stateFilter: string;
  onFilter: (value: string) => void;
  counts: Record<string, number>;
  total: number;
  onCreate: () => void;
}) {
  return (
    <div className="col">
      <div className="ch">
        <h3>
          会话
          <span style={{ marginLeft: "auto", fontSize: 11.5, color: "var(--ink3)", fontWeight: 400 }}>
            共 {total} 条
          </span>
        </h3>
        {/* 按**状态**筛选,不按模式:模式已经是每行上的静态标签,再来一个模式筛选器
            就是两个互相矛盾的控件。计数让人不必挨个点开才知道哪里有积压。 */}
        <div className="chips" style={{ marginTop: 9, gap: 6 }}>
          {STATE_FILTERS.map(([value, label]) => (
            <span
              key={value || "all"}
              className={stateFilter === value ? "on" : ""}
              onClick={() => onFilter(value)}
              style={{ padding: "3px 10px", fontSize: 11.5 }}
            >
              {label}
              {value ? ` ${counts[value] ?? 0}` : ""}
            </span>
          ))}
        </div>
        {/* 新建会话是这个列表自己的动作,不是页面的——放在筛选和卡片之间,进页面就在
            视野里,不用先看完页头才找到入口。 */}
        <button className="btn sm pri" style={{ marginTop: 8, width: "100%" }} onClick={onCreate}>
          ＋ 新建会话
        </button>
      </div>

      <div className="cb">
        {sessions.length === 0 ? (
          <Empty>没有匹配的会话</Empty>
        ) : (
          sessions.map((session) => (
            <div
              key={session.id}
              className={picked === session.id ? "ses on" : "ses"}
              onClick={() => onPick(session.id)}
            >
              <div className="r1" style={{ display: "flex", gap: 5, alignItems: "center" }}>
                <Tag tone={permissionTag(session.writable).tone}>
                  {permissionTag(session.writable).label}
                </Tag>
                {/* 关了任务的会话把任务标出来:现在**任何**会话都可能关任务,不再是
                    某一种模式的专属。 */}
                {session.task_id && <Tag tone="mute">{session.task_id}</Tag>}
                <span style={{ marginLeft: "auto" }}>
                  <Tag
                    tone={
                      session.state === "active"
                        ? "ok"
                        : session.state === "suspended"
                          ? "warn"
                          : "mute"
                    }
                  >
                    {STATE_LABEL[session.state] ?? session.state}
                  </Tag>
                </span>
              </div>
              <b>{session.title || session.id}</b>
              {/* 显示名而不是 id——`dev-employee` 不是人话。时间让人认出哪些是刚聊过的。 */}
              <div className="m mono">
                {session.employee_name || session.employee_id}
                {session.task_id ? ` · ${session.task_id}` : ""}
                {` · ${clockOf(session.updated_at)}`}
              </div>
              {/* 标题重复时,这一行是认出「是哪一场」的依据。 */}
              {session.last_question && <div className="q">{session.last_question}</div>}
              {/* 挂起要说明原因,否则用户只会觉得「它坏了」。 */}
              {session.suspend_reason && (
                <div className="m">
                  {SUSPEND_LABEL[session.suspend_reason] ?? session.suspend_reason}
                </div>
              )}
            </div>
          ))
        )}
      </div>

      <div className="cf">
        <Note>
          模式在<b>创建时确定,之后不可变</b> —— 只读与可写是两套工具集,中途切换等于一次
          没有闸门的提权。
        </Note>
      </div>
    </div>
  );
}

// --- 对话流 -------------------------------------------------------------------

/** 草稿存 `localStorage`,**按会话 id 分键**。误刷新不该丢掉打了一半的问题。 */
const DRAFT_PREFIX = "agentgenome:chat-draft:";

function useDraft(sessionId: string): [string, (value: string) => void] {
  const key = `${DRAFT_PREFIX}${sessionId}`;
  const [draft, setDraftState] = useState(() => {
    try {
      return window.localStorage.getItem(key) ?? "";
    } catch {
      // 隐私模式下 localStorage 会抛。**草稿丢了顶多重打一遍,而抛出去会白屏。**
      return "";
    }
  });

  // 换会话时读那个会话自己的草稿——共用一个键的话,在 A 里打一半切到 B 会看到 A 的字。
  useEffect(() => {
    try {
      setDraftState(window.localStorage.getItem(key) ?? "");
    } catch {
      setDraftState("");
    }
  }, [key]);

  const setDraft = (value: string) => {
    setDraftState(value);
    try {
      if (value) window.localStorage.setItem(key, value);
      else window.localStorage.removeItem(key);
    } catch {
      // 同上:存不下不影响这一次对话。
    }
  };

  return [draft, setDraft];
}

/**
 * 关联任务那一行。**质询会话的全部意义就是围绕某个任务**,而这一行以前完全没有——用户
 * 看不到它质询的是什么,也点不过去。
 */
function LinkedTask({
  session,
  onOpenTask,
}: {
  session: SessionSummary | null;
  onOpenTask?: (taskId: string) => void;
}) {
  const taskId = session?.task_id ?? "";
  const [state, setState] = useState("");

  useEffect(() => {
    if (!taskId) return;
    // **拉不到就只显示 id 不显示状态。** 显示一个错的状态比不显示更坏——用户会据此
    // 判断该不该现在去看它。
    api
      .task(taskId)
      .then((task) => setState(task.state))
      .catch(() => setState(""));
  }, [taskId]);

  if (!session || !taskId) return null;

  return (
    <div className="m" style={{ marginBottom: 8, display: "flex", gap: 6, alignItems: "center" }}>
      关联任务{" "}
      <a className="mono" onClick={() => onOpenTask?.(taskId)}>
        {taskId} ›
      </a>
      {state && <Tag tone={stateTone(state)}>{state}</Tag>}
      {/* 这句话只对只读会话成立:可写会话是在这个任务的工作区里改,不是来读它的产物的。 */}
      {!session.writable && <span>· 预载了该任务的全部产物与日志</span>}
    </div>
  );
}

function Conversation({
  sessionId,
  onChanged,
  onOpenTask,
}: {
  sessionId: string;
  onChanged: () => void;
  onOpenTask?: (taskId: string) => void;
}) {
  const { session, blocks, state, error, send, retry, stop } = useSession(sessionId);
  const [draft, setDraft] = useDraft(sessionId);
  const [taskDraft, setTaskDraft] = useState<TaskDraft | null>(null);

  const busy = state === "sending" || state === "streaming" || state === "tool-running";

  const submit = () => {
    if (!draft.trim() || busy) return;
    const message = draft;
    setDraft("");
    void send(message).then(onChanged);
  };

  // 上一条**用户**消息。重新生成就是把它再发一次。
  const lastAsked = [...blocks]
    .reverse()
    .find((block) => (block.detail as Record<string, unknown> | undefined)?.["role"] === "user")
    ?.text;

  const regenerate = () => {
    if (!lastAsked || busy) return;
    void send(lastAsked).then(onChanged);
  };

  return (
    // 跟会话列表一样用 `.col`(固定高度 + 内部滚动),不再用通用 `.card`(随内容无限撑高)
    // ——否则消息一多,中栏没有自己的滚动条,只能靠整页跟着滚,输入框和顶部上下文条也会被
    // 一起卷走。`.ch` 顶栏、`.ctx` 上下文条、`.cf` 输入区三处**钉住**,只有 `.cb` 里的消息滚动。
    <div className="col">
      <div className="ch">
        <h3>
          {session?.title || sessionId}
          {session && (
            <span style={{ marginLeft: "auto", display: "flex", gap: 6, alignItems: "center" }}>
              {/* 静态标签,不是 Tab —— 界面不该暗示一个后端拒绝执行的操作。 */}
              <Tag tone={permissionTag(session.writable).tone}>
                {permissionTag(session.writable).label}
              </Tag>
              {/* 会话 id 摆出来,人要把它贴给别人时不必去地址栏里翻。 */}
              <span className="m mono">{session.id}</span>
            </span>
          )}
        </h3>
        <LinkedTask session={session} onOpenTask={onOpenTask} />
      </div>

      <ContextBar session={session} onChanged={onChanged} />

      <div className="cb">
        <MessageList
          blocks={blocks}
          employeeName={session?.employee_name || session?.employee_id || "员工"}
          onOpenTask={onOpenTask}
        />

        {state === "error" && (
          // 错误块内联呈现,不弹全局 toast 打断心流。
          <Note tone="warn">
            {error}{" "}
            <button className="btn sm" onClick={() => void retry()}>
              重试
            </button>
          </Note>
        )}

        {taskDraft && <DraftCard draft={taskDraft} onClose={() => setTaskDraft(null)} />}
      </div>

      {/* **已结束的会话收起输入区。** 给一个发不出去的输入框,用户会打完一段话才发现。
          已挂起**不算已结束**:它可以恢复,所以输入区照常给——「已结束」是不可恢复的那一档,
          两者合并的话,可恢复的会话会被无故剥夺输入。 */}
      {session?.state === "ended" ? (
        <div className="cf">
          <Note>这个会话已结束,不能再发消息。上面的记录与证据仍然可查。</Note>
        </div>
      ) : (
        <div className="cf">
          <div className="composer">
            <textarea
              aria-label="输入消息"
              placeholder="继续追问…… @员工 引用同事 #任务 引用任务 /指令"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
            <div className="crow">
              {/* **只插前缀,不假装有补全。** 真正的 @ 补全面板是另一件事;给一个点了
                  没反应的补全,比不给更糟。 */}
              <button className="btn sm" onClick={() => setDraft(`${draft}@`)}>
                @员工
              </button>
              <button className="btn sm" onClick={() => setDraft(`${draft}/`)}>
                /指令
              </button>
              <span className="m">{STATE_HINT[state]}</span>
              <span className="sp" />
              {busy && (
                <button className="btn sm bad" onClick={stop}>
                  停止生成
                </button>
              )}
              {/* **重新生成不是重试。** 重试是上一轮失败了;重新生成是上一轮成功了但答得
                  不好。两者在状态机里的入口不同,文案也不该混。 */}
              <button className="btn sm" disabled={busy || !lastAsked} onClick={regenerate}>
                ↻ 重新生成
              </button>
              <button className="btn sm" onClick={() => void escalate(sessionId, setTaskDraft)}>
                转为任务
              </button>
              <button className="btn sm pri" disabled={busy} onClick={submit}>
                发送 ➤
              </button>
            </div>
          </div>
          {/* 预算与超时必须可见,否则用户会毫无征兆地被挂起。 */}
          {blocks.length > 0 && <Feedback sessionId={sessionId} />}
          {session && (
            <div className="budget">
              <span>会话预算</span>
              {/* **不限时不画进度条。** 一条永远空着的槽会被读成"快用完了还是没开始",
                  而"不限"这件事本身没有进度可言。 */}
              {unlimitedBudget(session.max_tokens) ? (
                <span>{`已用 ${shortTokens(session.tokens_used)} tokens · 不限`}</span>
              ) : (
                <>
                  {/* 进度条:`12400 / 50000` 要用户自己数位数才知道还剩多少。 */}
                  <span className="bar" aria-label="预算用量">
                    <i style={{ width: `${budgetPercent(session)}%` }} />
                  </span>
                  <span>{`${shortTokens(session.tokens_used)} / ${shortTokens(session.max_tokens)} tokens`}</span>
                </>
              )}
              <BudgetSetting onSaved={onChanged} />
              <span>· 空闲 {Math.round((session.idle_timeout_s ?? 0) / 60)} 分钟自动挂起</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 会话 token 上限的配置面板。**就摆在预算条旁边。**
 *
 * 放在这里而不是某个设置页:人想起要改这一项的唯一时刻,就是他正看着这条预算条、或者刚被
 * 它挂起。要他记住去别的页面找一个叫 `budgets.session_tokens` 的字段,等于这个旋钮不存在。
 *
 * 写的是**项目级配置**(`budgets` 段),因此走的是与设置页同一条 `PUT /settings`——内容
 * 进 git、动作与操作人进事件面,一样不少。这也是为什么这里不另开一个"会话预算"接口:
 * 另开一条就等于给同一份配置开了第二条没有审计的写入路径。
 *
 * 它对**之后新建的会话**生效;正被预算挂起的那个,恢复时会按新值重取(后端 `resume`)。
 */
function BudgetSetting({ onSaved }: { onSaved: () => void }) {
  const [limit, setLimit] = useState<number | null>(null);
  const [canEdit, setCanEdit] = useState(false);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [failed, setFailed] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .settings()
      .then((found) => {
        setLimit(found.budgets.session_tokens ?? 0);
        setCanEdit(found.can_edit);
      })
      // 读不到配置不该让预算条整块消失——它显示的是会话自己的用量,与能不能改配置无关。
      .catch(() => setCanEdit(false));
  }, []);

  const save = () => {
    if (limit === null || busy) return;
    // 空串 = 不限。**留一个明确的"清空即不限"**,而不是逼用户去打一个 0——"0" 看起来
    // 像"一个 token 都不给"。
    const wanted = draft.trim() === "" ? 0 : Number(draft.trim());
    if (!Number.isInteger(wanted) || wanted < 0) {
      setFailed("要一个不小于 0 的整数;留空表示不限。");
      return;
    }
    setBusy(true);
    setFailed("");
    api
      .settings()
      .then((found) =>
        // **整段回写**:`PUT /settings` 按段覆盖,只发一个字段会把同段的另外两项
        // (每任务/每 Job 预算)一起写成默认值。
        api.updateSettings("budgets", { ...found.budgets, session_tokens: wanted }),
      )
      .then(() => {
        setLimit(wanted);
        setOpen(false);
        onSaved();
      })
      .catch((e: ApiError) => setFailed(e.detail))
      .finally(() => setBusy(false));
  };

  if (limit === null || !canEdit) return null;

  if (!open) {
    return (
      <button
        className="btn sm"
        onClick={() => {
          setDraft(limit ? String(limit) : "");
          setFailed("");
          setOpen(true);
        }}
      >
        设置上限
      </button>
    );
  }

  return (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
      <label htmlFor="budget-limit">每个会话上限</label>
      <input
        id="budget-limit"
        style={{ width: 110 }}
        value={draft}
        placeholder="留空 = 不限"
        onChange={(event) => setDraft(event.target.value)}
      />
      <span className="m">tokens</span>
      <button className="btn sm pri" disabled={busy} onClick={save}>
        保存
      </button>
      <button className="btn sm" disabled={busy} onClick={() => setOpen(false)}>
        取消
      </button>
      {failed && <span className="m">{failed}</span>}
    </span>
  );
}

const STATE_HINT: Record<string, string> = {
  idle: "",
  sending: "发送中…",
  streaming: "正在回答…",
  "tool-running": "正在查证…",
  done: "",
  error: "出错了",
};

/**
 * 消息列表。**只渲染尾部的一段**,上面的按需展开。
 *
 * ## 为什么是"载入更早的",不是虚拟滚动
 *
 * 虚拟滚动要测量每一项的高度,而这里的项高度差着数量级——一行文本 vs 一整份 diff vs
 * 一张门禁表。测不准的后果是滚动条乱跳,比不做还糟。
 *
 * 会话的读法本来就是"看最新的,偶尔往回翻",与日志那种要跳到任意位置的场景不同。所以先
 * 用窗口 + 显式展开:它对高度不作任何假设,而且**用户知道上面还有多少**。
 *
 * PRD 28 把长会话密度标成未解问题,并说"实现前先拿真实会话长度试一版"。这一版是那句话
 * 的兑现方式——不假装解决了,但也不让几百条消息一次全渲染。
 */
const WINDOW = 40;

function MessageList({
  blocks,
  employeeName,
  onOpenTask,
}: {
  blocks: BlockItem[];
  employeeName: string;
  onOpenTask?: (taskId: string) => void;
}) {
  const [limit, setLimit] = useState(WINDOW);
  const hidden = Math.max(0, blocks.length - limit);
  const shown = hidden > 0 ? blocks.slice(hidden) : blocks;

  return (
    // 不再自己开滚动区——外层 `.cb` 已经是这一栏唯一的滚动容器,这里再套一层
    // `overflow:auto` 只会切出第二个滚动条,composer 下面的 `.cf` 却纹丝不动。
    <div style={{ padding: "4px 0" }}>
      {blocks.length === 0 ? (
        <Empty>还没有消息 —— 问点什么</Empty>
      ) : (
        <>
          {hidden > 0 && (
            <div className="older">
              <button onClick={() => setLimit((value) => value + WINDOW)}>
                ↑ 载入更早的 {Math.min(hidden, WINDOW)} 条(还有 {hidden} 条)
              </button>
            </div>
          )}
          {groupBlocks(shown).map((entry) =>
            Array.isArray(entry) ? (
              <div key={entry[0]!.seq} className="msg a">
                <div className="bd">
                  <BlockRun blocks={entry} />
                </div>
              </div>
            ) : (
              <Message
                key={entry.seq}
                block={entry}
                employeeName={employeeName}
                onOpenTask={onOpenTask}
              />
            ),
          )}
        </>
      )}
    </div>
  );
}

function Message({
  block,
  employeeName,
  onOpenTask,
}: {
  block: BlockItem;
  employeeName: string;
  onOpenTask?: (id: string) => void;
}) {
  const detail = block.detail as Record<string, unknown> | undefined;
  const mine = detail?.["role"] === "user";
  if (mine) {
    return (
      <div className="msg u">
        <div className="bb">{block.text}</div>
      </div>
    );
  }
  // **署名**:长对话里没有头像/名字/时间就分不清段落归属,几屏之后全是一片连着的文字。
  const at = typeof detail?.["at"] === "string" ? (detail["at"] as string) : "";
  return (
    <div className="msg a">
      <div className="hd">
        <span className="a">{employeeName.slice(0, 1) || "员"}</span>
        <b>{employeeName}</b>
        {at && <span>{at}</span>}
      </div>
      <div className="bd">
        <BlockView block={block} onOpenTask={onOpenTask} />
      </div>
    </div>
  );
}

/**
 * 上下文条:**输入面** —— 员工带着什么在回答。
 *
 * 住在对话栏**内部**,不跨页面横排:它是这个会话的属性,不是页面的;而且横跨三栏会让
 * 页面标题 + 上下文条叠起来吃掉近半屏。
 *
 * chip 来自后端的 `context_items`,而那份清单**减掉了被预算砍掉的**——把没真的进上下文
 * 的卡片列进"已装载"是在骗人,而用户正是看着这个控件判断该不该采信这次回答。
 */
function ContextBar({
  session,
  onChanged,
}: {
  session: SessionSummary | null;
  onChanged: () => void;
}) {
  if (!session) return null;
  const items = session.context_items ?? [];
  const pinned = new Set(session.pinned ?? []);

  return (
    // 用设计稿的 `.ctx` / `.cp`:紧凑胶囊 + 图钉,而不是每个条目挂两个文字按钮。
    // 条目多起来时,文字按钮会让这一条吃掉半屏——而它只是"员工带着什么"的一个提示。
    <div className="ctx">
      <div className="lb">
        🧩 <b>已装载</b>(输入面 · 可增删 · 可钉住)
      </div>
      {items.length === 0 ? (
        <span className="m">这次没有装载任何材料</span>
      ) : (
        <div className="cs">
          {items.map((item) => (
            <span key={item} className={pinned.has(item) ? "cp pin" : "cp"}>
              {/* 图钉本身就是开关——钉住/取消钉住不必各占一个文字按钮。 */}
              <i
                role="button"
                aria-label={`钉住 ${item}`}
                onClick={() => api.pinContext(session.id, item, !pinned.has(item)).then(onChanged)}
              >
                {pinned.has(item) ? "📌" : "📍"}
              </i>
              {item}
              <i
                role="button"
                aria-label={`移除 ${item}`}
                onClick={() => api.dropContext(session.id, item).then(onChanged)}
              >
                ×
              </i>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * 有用/没用。
 *
 * **这是「对话本身成为知识自然选择的输入源」唯一的落地点**(§10.2)。没有它,那句话
 * 只是一句主张。记账在后端做——前端不自行改任何 hits 显示值。
 */
function Feedback({ sessionId }: { sessionId: string }) {
  const [said, setSaid] = useState("");

  const say = (useful: boolean) =>
    api
      .sessionFeedback(sessionId, useful)
      .then((result) => {
        const credited = result.credited ?? [];
        setSaid(
          credited.length
            ? `已记账:${credited.join("、")} hits +1`
            : useful
              ? "谢谢——这次回答没带功能卡片,没有可记账的"
              : "记下了",
        );
      })
      .catch(() => setSaid("反馈没送出去"));

  return (
    <div className="m" style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 6 }}>
      这条回答有用吗?
      <button className="btn sm" onClick={() => say(true)}>👍 有用</button>
      <button className="btn sm" onClick={() => say(false)}>👎 没用</button>
      {said && <span style={{ marginLeft: "auto" }}>{said}</span>}
    </div>
  );
}

/**
 * 转任务的内联草稿卡。
 *
 * **出现在对话流里而不是弹窗**——弹窗会盖住上文,而用户正需要对照上文改草稿。
 * **确认前不建任务**:自动建任务会让「随口问一句」变成「莫名多了个任务」。
 */
function DraftCard({ draft, onClose }: { draft: TaskDraft; onClose: () => void }) {
  const [title, setTitle] = useState(draft.title);
  const [requirement, setRequirement] = useState(draft.requirement);
  const [done, setDone] = useState("");

  return (
    <Card title="任务草稿 · 尚未提交">
      <label>标题</label>
      <input value={title} onChange={(event) => setTitle(event.target.value)} />
      <label>需求</label>
      <textarea value={requirement} onChange={(event) => setRequirement(event.target.value)} />
      <div style={{ display: "flex", gap: 6, marginTop: 10, alignItems: "center" }}>
        <button
          className="btn sm pri"
          onClick={() =>
            api
              .submit({
                title,
                requirement,
                priority: 5,
                itest: "auto",
                mode: "autonomous",
                // 从对话转出来的任务跟随项目缺省策略。**这里不该出现第二个选择器**:
                // 咨询会话谈的是"要做什么",不是"用几路去做"。
                topology: "",
                // 建出来的任务记得住它是从哪次对话来的。
                source_session_id: draft.source_session_id,
              })
              .then((task) => setDone(`已建任务 ${task.id}`))
              .catch((e: ApiError) => setDone(e.detail))
          }
        >
          确认建任务
        </button>
        <button className="btn sm" onClick={onClose}>
          取消
        </button>
        <span className="m">确认前不写入任何任务</span>
      </div>
      {done && <Note>{done}</Note>}
    </Card>
  );
}

async function escalate(
  sessionId: string,
  setDraft: (draft: TaskDraft | null) => void,
): Promise<void> {
  try {
    setDraft(await api.escalateSession(sessionId));
  } catch {
    setDraft(null);
  }
}

// --- 证据面板 -----------------------------------------------------------------

/**
 * 证据面板:**输出面** —— 这次对话引用过什么,只累积不可改。
 *
 * 与上下文条的区别要看得见,否则用户会以为同一份东西显示了两遍。
 */
function Evidence({ session }: { session: SessionSummary | null }) {
  const sessionId = session?.id ?? "";
  const [blocks, setBlocks] = useState<BlockItem[]>([]);
  const [tab, setTab] = useState<"evidence" | "about">("evidence");

  useEffect(() => {
    if (!sessionId) return;
    api
      .sessionMessages(sessionId)
      .then((page) => setBlocks(page.items))
      .catch(() => setBlocks([]));
  }, [sessionId]);

  const cards = blocks.filter((block) => block.kind === "card-ref");
  const files = blocks.filter((block) => block.kind === "file-ref");
  // 任务产物:**只列这次对话引用过的**,不是任务的全部产物——证据面板是"这次对话引用了
  // 什么",不是任务详情的副本。
  const artifacts = blocks.filter((block) => block.kind === "artifact-ref");

  return (
    // 同中栏——`.col` 固定高度,`.ch` 里的 tab 钉住,`.cb` 里的证据列表自己滚动。
    <div className="col">
      <div className="ch">
        <h3>证据面板</h3>
        <div className="tabs" style={{ margin: "9px 0 0", borderBottom: 0 }}>
          <button className={tab === "evidence" ? "on" : ""} onClick={() => setTab("evidence")}>
            引用证据
          </button>
          <button className={tab === "about" ? "on" : ""} onClick={() => setTab("about")}>
            会话信息
          </button>
        </div>
      </div>

      <div className="cb">
      {tab === "evidence" ? (
        <>
          <div className="m" style={{ margin: "8px 0" }}>
            <b>输出面</b> —— 本次对话引用过的材料,只读累积
          </div>
          <EvidenceGroup label="功能卡片" count={cards.length}>
            {cards.map((block) => {
              const detail = (block.detail ?? {}) as Record<string, unknown>;
              return (
                <div key={block.seq} className="ev">
                  <b>{String(detail["title"] ?? block.text)}</b>
                  <div className="m">
                    {/* 命中与置信度正是判断该不该采信这次回答的依据,只给名字等于没给。 */}
                    {detail["hits"] !== undefined && `命中 ${detail["hits"]} 次`}
                    {detail["confidence"] ? ` · 置信度 ${detail["confidence"]}` : ""}
                    {detail["updated_at"] ? ` · 更新于 ${detail["updated_at"]}` : ""}
                  </div>
                </div>
              );
            })}
          </EvidenceGroup>
          <EvidenceGroup label="文件" count={files.length}>
            {files.map((block) => (
              <div key={block.seq} className="ev">
                <span className="mono">{block.text}</span>
              </div>
            ))}
          </EvidenceGroup>
          <EvidenceGroup label="任务产物" count={artifacts.length}>
            {artifacts.map((block) => {
              const detail = (block.detail ?? {}) as Record<string, unknown>;
              return (
                <div key={block.seq} className="ev">
                  <span className="mono">{block.text}</span>
                  {detail["at"] ? <div className="m">{String(detail["at"])} 生成</div> : null}
                </div>
              );
            })}
          </EvidenceGroup>
          {cards.length + files.length + artifacts.length === 0 && (
            <Empty>还没有引用任何材料</Empty>
          )}
        </>
      ) : (
        <div style={{ marginTop: 8 }}>
          {/* 这些元信息以前没有地方可查。 */}
          <div className="kv">
            <span className="k">员工</span>
            <span className="v">{session?.employee_name || session?.employee_id}</span>
          </div>
          <div className="kv">
            <span className="k">权限</span>
            <span className="v">{permissionTag(!!session?.writable).label}</span>
          </div>
          <div className="kv">
            <span className="k">关联任务</span>
            <span className="v mono">{session?.task_id || "无"}</span>
          </div>
          <div className="kv">
            <span className="k">创建于</span>
            <span className="v">{session?.created_at}</span>
          </div>
          <div className="kv">
            <span className="k">会话 id</span>
            <span className="v mono">{session?.id}</span>
          </div>
        </div>
      )}
      </div>
    </div>
  );
}

/** 一组证据。**空组也显示计数**——「文件 0」和"这一组不存在"是两回事。 */
function EvidenceGroup({
  label,
  count,
  children,
}: {
  label: string;
  count: number;
  children: ReactNode;
}) {
  return (
    <div className="grp">
      <div className="gh">
        <b>{label}</b> {count}
      </div>
      {children}
    </div>
  );
}
