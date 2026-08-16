import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { Chat } from "./Chat";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    api: {
      sessions: vi.fn(),
      session: vi.fn(),
      sessionMessages: vi.fn(),
      escalateSession: vi.fn(),
      task: vi.fn(),
      sendSessionMessage: vi.fn(),
      attachSessionStream: vi.fn(),
      stopSession: vi.fn(),
      submit: vi.fn(),
      createSession: vi.fn(),
      employees: vi.fn(),
      tasks: vi.fn(),
      pinContext: vi.fn(),
      dropContext: vi.fn(),
      sessionFeedback: vi.fn(),
      settings: vi.fn(),
      updateSettings: vi.fn(),
    },
  };
});

const mocked = api as unknown as {
  sessions: ReturnType<typeof vi.fn>;
  session: ReturnType<typeof vi.fn>;
  sessionMessages: ReturnType<typeof vi.fn>;
  escalateSession: ReturnType<typeof vi.fn>;
  task: ReturnType<typeof vi.fn>;
  sendSessionMessage: ReturnType<typeof vi.fn>;
  attachSessionStream: ReturnType<typeof vi.fn>;
  stopSession: ReturnType<typeof vi.fn>;
  submit: ReturnType<typeof vi.fn>;
  employees: ReturnType<typeof vi.fn>;
  tasks: ReturnType<typeof vi.fn>;
  pinContext: ReturnType<typeof vi.fn>;
  dropContext: ReturnType<typeof vi.fn>;
  sessionFeedback: ReturnType<typeof vi.fn>;
  settings: ReturnType<typeof vi.fn>;
  updateSettings: ReturnType<typeof vi.fn>;
};

const SESSION = {
  id: "sess-20260810-001",
  employee_id: "dev-employee",
  employee_name: "开发员工",
  last_question: "这个补偿逻辑有超时保护吗?",
  writable: false,
  state: "active",
  generating: false,
  title: "预占补偿逻辑",
  task_id: null,
  suspend_reason: null,
  tokens_used: 1200,
  max_tokens: 50000,
  idle_timeout_s: 1800,
  last_seq: 2,
  context_items: ["card:order-service/reserve-flow", "task:ag-1"],
  pinned: [],
  created_at: "2026-08-10T10:00:00Z",
  updated_at: "2026-08-10T10:05:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
  mocked.sessions.mockResolvedValue({
    items: [SESSION],
    counts: { active: 1, suspended: 2, ended: 16 },
    total: 19,
  });
  mocked.session.mockResolvedValue(SESSION);
  mocked.employees.mockResolvedValue({
    items: [
      { id: "arch-employee", runtime: "claude-code", can_session: true, session_blocked_reason: "" },
    ],
  });
  mocked.tasks.mockResolvedValue([]);
  mocked.task.mockResolvedValue({ id: "ag-20260809-002", state: "REVIEWING" });
  mocked.sendSessionMessage.mockResolvedValue(undefined);
  mocked.attachSessionStream.mockResolvedValue(undefined);
  mocked.stopSession.mockResolvedValue(SESSION);
  mocked.settings.mockResolvedValue({
    can_edit: true,
    budgets: { per_task_tokens: 1500000, per_job_tokens: 300000, session_tokens: 0 },
  });
  mocked.updateSettings.mockResolvedValue({ rev: "abc", section: "budgets" });
  mocked.sessionMessages.mockResolvedValue({
    session_id: SESSION.id,
    items: [
      { seq: 1, kind: "text", text: "补偿逻辑是什么?", detail: { role: "user" } },
      { seq: 2, kind: "text", text: "超时后由补偿任务回滚", detail: {} },
    ],
    last_seq: 2,
  });
});

describe("对话工作台", () => {
  it("lists sessions with their permission as a static label", async () => {
    render(<Chat />);

    expect(await screen.findAllByText("只读")).not.toHaveLength(0);
  });

  it("shows the permission as text, never as a control that could change it", async () => {
    // 后端没有任何改读写权限的 API。界面提供一个看起来能切的控件,等于暗示一次
    // 不经闸门的提权是可能的。
    render(<Chat />);
    await screen.findAllByText("只读");

    const buttons = screen.getAllByRole("button").map((button) => button.textContent);
    expect(buttons).not.toContain("只读");
    expect(buttons).not.toContain("可写");
  });

  it("filters by state, not by permission", async () => {
    // 权限已经是每行上的静态标签,再来一个权限筛选器就是两个互相矛盾的控件。
    render(<Chat />);

    expect(await screen.findByText(/^进行中 \d+$/)).toBeInTheDocument();
    expect(await screen.findByText(/^已挂起 \d+$/)).toBeInTheDocument();
    expect(await screen.findByText(/^已结束 \d+$/)).toBeInTheDocument();
  });

  it("counts every state on the filters, not just the one in view", async () => {
    // 计数回答的是「别处有没有积压」。拿筛完的结果去数的话,选中「进行中」之后别的几项
    // 会一起变成 0——而那正是这些数字要回答的问题,方向相反。
    render(<Chat />);

    expect(await screen.findByText("已挂起 2")).toBeInTheDocument();
    expect(await screen.findByText("已结束 16")).toBeInTheDocument();
    expect(await screen.findByText("共 19 条")).toBeInTheDocument();
  });

  it("shows who and when, and a preview of the last question", async () => {
    // 标题重复时,预览是认出「是哪一场」的依据;显示名让列表读起来是人话。
    render(<Chat />);

    expect(await screen.findByText(/开发员工/)).toBeInTheDocument();
    expect(await screen.findByText("这个补偿逻辑有超时保护吗?")).toBeInTheDocument();
  });

  it("renders the conversation blocks", async () => {
    render(<Chat />);

    expect(await screen.findByText("超时后由补偿任务回滚")).toBeInTheDocument();
  });

  it("keeps the budget and idle timeout visible", async () => {
    // 不露出来的话用户会毫无征兆地被挂起。
    render(<Chat />);

    // 短形式:`1200 / 50000` 要用户自己数位数才知道还剩多少。
    expect(await screen.findByText("1.2k / 50k tokens")).toBeInTheDocument();
    expect(await screen.findByText(/30 分钟自动挂起/)).toBeInTheDocument();
  });

  it("says 不限 instead of drawing an empty bar when there is no limit", async () => {
    // **0 = 不限,不是"上限是 0"。** 画一条永远空着的槽会被读成"快用完了还是没开始",
    // 而"不限"这件事本身没有进度可言。
    mocked.session.mockResolvedValue({ ...SESSION, max_tokens: 0 });

    render(<Chat />);

    expect(await screen.findByText(/已用 1\.2k tokens · 不限/)).toBeInTheDocument();
    expect(screen.queryByLabelText("预算用量")).not.toBeInTheDocument();
  });

  it("shows the budget as a bar, not just a number", async () => {
    render(<Chat />);

    const bar = await screen.findByLabelText("预算用量");
    // 1200 / 50000 ≈ 2%
    expect(bar.firstElementChild).toHaveStyle({ width: "2%" });
  });

  it("separates the context bar (input) from the evidence panel (output)", async () => {
    render(<Chat />);

    expect(await screen.findByText(/已装载/)).toBeInTheDocument();
    expect(await screen.findByText(/只读累积/)).toBeInTheDocument();
  });

  it("renders one chip per loaded item, not a blanket sentence", async () => {
    // 「员工带着什么在回答」要具体到条目。一句笼统的话等于没说。
    render(<Chat />);

    expect(await screen.findByText(/card:order-service\/reserve-flow/)).toBeInTheDocument();
    expect(await screen.findByText(/task:ag-1/)).toBeInTheDocument();
  });

  it("can pin an item so it survives truncation", async () => {
    mocked.pinContext.mockResolvedValue(SESSION);
    render(<Chat />);
    await screen.findByText(/card:order-service\/reserve-flow/);

    await userEvent.click(screen.getByLabelText("钉住 card:order-service/reserve-flow"));

    expect(mocked.pinContext).toHaveBeenCalledWith(
      SESSION.id,
      "card:order-service/reserve-flow",
      true,
    );
  });

  it("can drop an item the employee was led astray by", async () => {
    mocked.dropContext.mockResolvedValue(SESSION);
    render(<Chat />);
    await screen.findByText(/task:ag-1/);

    await userEvent.click(screen.getByLabelText("移除 task:ag-1"));

    expect(mocked.dropContext).toHaveBeenCalledWith(SESSION.id, "task:ag-1");
  });

  it("says why a session was suspended", async () => {
    mocked.sessions.mockResolvedValue({
      items: [{ ...SESSION, state: "suspended", suspend_reason: "idle" }],
    });

    render(<Chat />);

    expect(await screen.findByText("空闲超时自动挂起")).toBeInTheDocument();
  });

  it("surfaces a load failure instead of looking empty", async () => {
    const { ApiError } = await vi.importActual<typeof import("../api/client")>("../api/client");
    mocked.sessions.mockRejectedValue(new ApiError(500, "库挂了"));

    render(<Chat />);

    expect(await screen.findByText(/库挂了/)).toBeInTheDocument();
  });
});

describe("预算上限的配置面板", () => {
  it("lets the user set the limit right where the budget is shown", async () => {
    // 人想起要改这一项的唯一时刻,就是他正看着这条预算条、或者刚被它挂起。要他记住去别的
    // 页面找一个叫 `budgets.session_tokens` 的字段,等于这个旋钮不存在。
    render(<Chat />);
    await userEvent.click(await screen.findByRole("button", { name: "设置上限" }));

    await userEvent.type(screen.getByLabelText("每个会话上限"), "200000");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      // **整段回写**:只发一个字段会把同段的另外两项一起写成默认值。
      expect(mocked.updateSettings).toHaveBeenCalledWith("budgets", {
        per_task_tokens: 1500000,
        per_job_tokens: 300000,
        session_tokens: 200000,
      }),
    );
  });

  it("treats an empty box as 不限, not as zero", async () => {
    // 逼用户去打一个 0 的话,"0" 看起来像"一个 token 都不给"。
    mocked.settings.mockResolvedValue({
      can_edit: true,
      budgets: { per_task_tokens: 1500000, per_job_tokens: 300000, session_tokens: 50000 },
    });
    render(<Chat />);
    await userEvent.click(await screen.findByRole("button", { name: "设置上限" }));

    await userEvent.clear(screen.getByLabelText("每个会话上限"));
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(mocked.updateSettings).toHaveBeenCalledWith(
        "budgets",
        expect.objectContaining({ session_tokens: 0 }),
      ),
    );
  });

  it("refuses a non-integer instead of writing something the backend will reject", async () => {
    render(<Chat />);
    await userEvent.click(await screen.findByRole("button", { name: "设置上限" }));

    await userEvent.type(screen.getByLabelText("每个会话上限"), "很多");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByText(/不小于 0 的整数/)).toBeInTheDocument();
    expect(mocked.updateSettings).not.toHaveBeenCalled();
  });

  it("hides the knob from someone who cannot edit settings", async () => {
    // 摆一个按下去必然 403 的按钮,等于让界面替后端撒谎。
    mocked.settings.mockResolvedValue({
      can_edit: false,
      budgets: { per_task_tokens: 1500000, per_job_tokens: 300000, session_tokens: 0 },
    });
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    expect(screen.queryByRole("button", { name: "设置上限" })).not.toBeInTheDocument();
  });
});

describe("反馈进化", () => {
  it("credits the loaded cards when the answer helped", async () => {
    // 这是「对话本身成为知识自然选择的输入源」唯一的落地点(§10.2)。
    mocked.sessionFeedback.mockResolvedValue({ credited: ["order-service/reserve-flow"] });
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    await userEvent.click(screen.getByText("👍 有用"));

    expect(mocked.sessionFeedback).toHaveBeenCalledWith(SESSION.id, true);
    expect(await screen.findByText(/hits \+1/)).toBeInTheDocument();
  });

  it("does not touch hits itself — the backend does the accounting", async () => {
    // 改 hits 是基因组的写操作,必须走后端既有路径。前端只把结果显示出来。
    mocked.sessionFeedback.mockResolvedValue({ credited: [] });
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    await userEvent.click(screen.getByText("👎 没用"));

    expect(mocked.sessionFeedback).toHaveBeenCalledWith(SESSION.id, false);
    expect(await screen.findByText("记下了")).toBeInTheDocument();
  });
});

describe("转为任务", () => {
  it("shows an editable draft and creates nothing until confirmed", async () => {
    // **确认前不建任务。** 自动建任务会让「随口问一句」变成「莫名多了个任务」。
    mocked.escalateSession.mockResolvedValue({
      title: "加一个审计字段",
      requirement: "- 加一个审计字段",
      modules: [],
      source_session_id: SESSION.id,
    });
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    await userEvent.click(screen.getByText("转为任务"));

    expect(await screen.findByDisplayValue("加一个审计字段")).toBeInTheDocument();
    expect(mocked.submit).not.toHaveBeenCalled();
  });

  it("carries the source session onto the created task", async () => {
    mocked.escalateSession.mockResolvedValue({
      title: "加一个审计字段",
      requirement: "- 加一个审计字段",
      modules: [],
      source_session_id: SESSION.id,
    });
    mocked.submit.mockResolvedValue({ id: "ag-20260810-009" });
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");
    await userEvent.click(screen.getByText("转为任务"));
    await screen.findByDisplayValue("加一个审计字段");

    await userEvent.click(screen.getByText("确认建任务"));

    await waitFor(() =>
      expect(mocked.submit).toHaveBeenCalledWith(
        expect.objectContaining({ source_session_id: SESSION.id }),
      ),
    );
  });

  it("creates the task only after the user confirms", async () => {
    mocked.escalateSession.mockResolvedValue({
      title: "加一个审计字段",
      requirement: "- 加一个审计字段",
      modules: [],
      source_session_id: SESSION.id,
    });
    mocked.submit.mockResolvedValue({ id: "ag-20260810-009" });
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");
    await userEvent.click(screen.getByText("转为任务"));
    await screen.findByDisplayValue("加一个审计字段");

    await userEvent.click(screen.getByText("确认建任务"));

    await waitFor(() => expect(mocked.submit).toHaveBeenCalled());
  });
});

describe("草稿", () => {
  beforeEach(() => window.localStorage.clear());

  it("survives a remount so a stray refresh does not lose it", async () => {
    const { unmount } = render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");
    await userEvent.type(screen.getByLabelText("输入消息"), "打了一半的问题");
    unmount();

    render(<Chat />);

    expect(await screen.findByDisplayValue("打了一半的问题")).toBeInTheDocument();
  });

  it("keeps drafts per session so switching does not leak text across", async () => {
    // 共用一个键的话,在 A 里打一半切到 B 会看到 A 的字。
    window.localStorage.setItem("agentgenome:chat-draft:other", "别的会话的草稿");
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    expect(screen.queryByDisplayValue("别的会话的草稿")).not.toBeInTheDocument();
  });
});

describe("长会话", () => {
  const many = Array.from({ length: 95 }, (_, index) => ({
    seq: index + 1,
    kind: "text",
    text: `第 ${index + 1} 条`,
    detail: {},
  }));

  it("only renders the tail so hundreds of messages do not all mount", async () => {
    mocked.sessionMessages.mockResolvedValue({ session_id: SESSION.id, items: many, last_seq: 95 });

    render(<Chat />);

    expect(await screen.findByText("第 95 条")).toBeInTheDocument();
    expect(screen.queryByText("第 1 条")).not.toBeInTheDocument();
  });

  it("tells the user how many are still hidden", async () => {
    // 「上面还有多少」要说出来。不说的话用户不知道自己看的是不是全部。
    mocked.sessionMessages.mockResolvedValue({ session_id: SESSION.id, items: many, last_seq: 95 });

    render(<Chat />);

    expect(await screen.findByText(/还有 55 条/)).toBeInTheDocument();
  });

  it("loads more when asked", async () => {
    mocked.sessionMessages.mockResolvedValue({ session_id: SESSION.id, items: many, last_seq: 95 });
    render(<Chat />);
    await screen.findByText("第 95 条");

    await userEvent.click(screen.getByText(/载入更早的/));

    expect(await screen.findByText("第 16 条")).toBeInTheDocument();
  });

  it("shows no load-more button when everything fits", async () => {
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    expect(screen.queryByText(/载入更早的/)).not.toBeInTheDocument();
  });
});

describe("新建会话入口", () => {
  it("has a visible way to start a session without knowing the shortcut", async () => {
    // 这一页以前没有任何新建入口:全仓唯一能建会话的地方是 ⌘J 抽屉,不知道快捷键的人
    // 站在这一页上找不到任何办法开始对话。
    render(<Chat />);

    expect(await screen.findByRole("button", { name: /新建会话/ })).toBeInTheDocument();
  });

  it("makes the empty-state sentence actually clickable", async () => {
    // 「还没有会话 —— 新建一个开始问」以前是纯文案,没有对应的可点控件。
    // **界面不该承诺一个不存在的动作。**
    mocked.sessions.mockResolvedValue({ items: [] });
    render(<Chat />);

    await userEvent.click(await screen.findByText("新建一个开始问"));

    expect(await screen.findByRole("option", { name: /arch-employee/ })).toBeInTheDocument();
  });

  it("opens the dialog from the button", async () => {
    render(<Chat />);

    await userEvent.click(await screen.findByRole("button", { name: /新建会话/ }));

    expect(await screen.findByLabelText(/让它改代码/)).toBeInTheDocument();
  });

  it("opens as an overlay without collapsing the page underneath", async () => {
    // 早先对话框顶掉了对话栏、还连带把证据面板一起藏了——新建时整页塌掉一半,而用户
    // 只是想开个会话。对话框该盖在页面上,不该改页面的骨架。
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");
    await userEvent.click(await screen.findByRole("button", { name: /新建会话/ }));

    await screen.findByLabelText(/让它改代码/);
    // 三栏都还在:会话列表、对话流、证据面板。
    expect(screen.getByText(/共 \d+ 条/)).toBeInTheDocument();
    expect(screen.getByText("证据面板")).toBeInTheDocument();
    expect(screen.getByText("超时后由补偿任务回滚")).toBeInTheDocument();
  });

  it("still offers no way to change the permission of an existing session", async () => {
    // **既有断言不许失效。** 新建时的权限开关如果被错误地放进已存在会话的展示区,
    // 这条会红——它守的是"界面不提供后端会拒绝的操作"。
    render(<Chat />);
    await screen.findAllByText("只读");

    const buttons = screen.getAllByRole("button").map((button) => button.textContent);
    expect(buttons).not.toContain("只读");
    expect(buttons).not.toContain("可写");
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});

describe("关联任务", () => {
  const INQUIRY = { ...SESSION, task_id: "ag-20260809-002" };

  it("shows the task this session is about, and lets you jump to it", async () => {
    // 质询会话的全部意义就是围绕某个任务,而这一行以前完全没有。
    mocked.sessions.mockResolvedValue({ items: [INQUIRY], counts: {}, total: 1 });
    mocked.session.mockResolvedValue(INQUIRY);
    const onOpenTask = vi.fn();
    render(<Chat onOpenTask={onOpenTask} />);

    await userEvent.click(await screen.findByText(/ag-20260809-002 ›/));

    expect(onOpenTask).toHaveBeenCalledWith("ag-20260809-002");
  });

  it("shows the linked task's current state", async () => {
    mocked.sessions.mockResolvedValue({ items: [INQUIRY], counts: {}, total: 1 });
    mocked.session.mockResolvedValue(INQUIRY);
    render(<Chat />);

    expect(await screen.findByText("REVIEWING")).toBeInTheDocument();
  });

  it("shows only the id when the task state cannot be fetched", async () => {
    // **显示一个错的状态比不显示更坏**——用户会据此判断该不该现在去看它。
    mocked.sessions.mockResolvedValue({ items: [INQUIRY], counts: {}, total: 1 });
    mocked.session.mockResolvedValue(INQUIRY);
    mocked.task.mockRejectedValue(new Error("拉不到"));
    render(<Chat />);

    expect(await screen.findByText(/ag-20260809-002 ›/)).toBeInTheDocument();
    expect(screen.queryByText("REVIEWING")).toBeNull();
  });

  it("does not claim a consult session preloaded task artifacts", async () => {
    // 咨询没有预载任何任务产物,对它说这句话是在骗人。
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    expect(screen.queryByText(/预载了该任务的全部产物与日志/)).toBeNull();
  });
});

describe("输入区", () => {
  it("can regenerate the last answer without retyping the question", async () => {
    // **重新生成不是重试。** 重试是上一轮失败了;重新生成是上一轮成功了但答得不好。
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    await userEvent.click(screen.getByRole("button", { name: /重新生成/ }));

    // 上一条用户消息被再发一次。
    await waitFor(() => expect(mocked.sendSessionMessage).toHaveBeenCalled());
  });

  it("offers the @员工 and /指令 entry points", async () => {
    render(<Chat />);

    expect(await screen.findByRole("button", { name: "@员工" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "/指令" })).toBeInTheDocument();
  });
});

describe("实时输出", () => {
  it("renders a block as soon as it streams in, without waiting for the whole turn", async () => {
    // **这是这次改动要修的问题本身。** 以前要等一整轮问完才一次性出现;现在应该边到边。
    let deliver: (block: unknown) => void = () => undefined;
    let finishTurn: () => void = () => undefined;
    mocked.sendSessionMessage.mockImplementation(
      (_id: string, _message: string, onBlock: (block: unknown) => void) =>
        new Promise<void>((resolve) => {
          deliver = onBlock;
          finishTurn = resolve;
        }),
    );
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    await userEvent.type(screen.getByLabelText("输入消息"), "新问题");
    await userEvent.click(screen.getByText("发送 ➤"));

    // 这一轮还没完(`finishTurn` 没调),但先到的这一块该已经在气泡里了。
    act(() => deliver({ seq: 3, kind: "text", text: "先说这一句", detail: {} }));
    expect(await screen.findByText("先说这一句")).toBeInTheDocument();

    act(() => finishTurn());
  });

  it("keeps the user's own message once the employee starts producing content", async () => {
    // **回归**:乐观块与权威回声的 seq 必然相同(前端的 lastSeq 与后端的 last_seq 恒等),
    // 而清理乐观块的过滤此前每收到一块就跑一次——第二块到达时摘掉的就是刚落位的权威
    // 回声,用户自己那句在员工开始输出的瞬间消失。
    //
    // **一块是测不出来的**:这个 bug 要第二块才触发,所以这条用例必须投两块。
    let deliver: (block: unknown) => void = () => undefined;
    let finishTurn: () => void = () => undefined;
    mocked.sendSessionMessage.mockImplementation(
      (_id: string, _message: string, onBlock: (block: unknown) => void) =>
        new Promise<void>((resolve) => {
          deliver = onBlock;
          finishTurn = resolve;
        }),
    );
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    await userEvent.type(screen.getByLabelText("输入消息"), "分析一下当前项目");
    await userEvent.click(screen.getByText("发送 ➤"));
    expect(await screen.findByText("分析一下当前项目")).toBeInTheDocument();

    // 后端先回声用户那句(seq 与乐观块相同),再开始产出内容。
    act(() => deliver({ seq: 3, kind: "text", text: "分析一下当前项目", detail: { role: "user" } }));
    act(() => deliver({ seq: 4, kind: "text", text: "我来看看这个项目", detail: {} }));

    expect(await screen.findByText("我来看看这个项目")).toBeInTheDocument();
    expect(screen.getByText("分析一下当前项目")).toBeInTheDocument();

    act(() => finishTurn());
  });

  it("reattaches to a turn that is still running when reopening the session", async () => {
    // 关页面/切走再切回来,发现上次那轮还没说完(`generating: true`)时,前端要自己
    // 接上直播,而不是傻等一条永远不会来的块。
    mocked.session.mockResolvedValue({ ...SESSION, generating: true });
    let deliver: (block: unknown) => void = () => undefined;
    mocked.attachSessionStream.mockImplementation(
      (_id: string, _after: number, onBlock: (block: unknown) => void) =>
        new Promise<void>(() => {
          deliver = onBlock; // 故意不 resolve——这一轮还在跑。
        }),
    );

    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    await waitFor(() =>
      expect(mocked.attachSessionStream).toHaveBeenCalledWith(
        SESSION.id,
        2, // 历史里最后一块的 seq——补齐从这里续。
        expect.any(Function),
        expect.anything(),
      ),
    );

    act(() => deliver({ seq: 3, kind: "text", text: "接着说下去", detail: {} }));
    expect(await screen.findByText("接着说下去")).toBeInTheDocument();
  });

  it("does not attach to anything when the session is not generating", async () => {
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    expect(mocked.attachSessionStream).not.toHaveBeenCalled();
  });

  it("stopping tells the backend to actually cancel the turn, not just hides it locally", async () => {
    mocked.sendSessionMessage.mockImplementation(() => new Promise<void>(() => undefined));
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");
    await userEvent.type(screen.getByLabelText("输入消息"), "新问题");
    await userEvent.click(screen.getByText("发送 ➤"));

    await userEvent.click(await screen.findByText("停止生成"));

    expect(mocked.stopSession).toHaveBeenCalledWith(SESSION.id);
  });
});

describe("证据面板", () => {
  it("separates evidence from session info with tabs", async () => {
    render(<Chat />);

    const panel = (await screen.findByText("证据面板")).closest(".col") as HTMLElement;

    await userEvent.click(within(panel).getByRole("button", { name: "会话信息" }));

    expect(within(panel).getByText("开发员工")).toBeInTheDocument();
    expect(within(panel).getByText("只读")).toBeInTheDocument();
  });

  it("groups evidence into cards, files and task artifacts", async () => {
    render(<Chat />);

    expect(await screen.findByText("任务产物")).toBeInTheDocument();
    expect(screen.getByText("功能卡片")).toBeInTheDocument();
    expect(screen.getByText("文件")).toBeInTheDocument();
  });
});

describe("会话回放", () => {
  const ENDED = { ...SESSION, state: "ended" };

  it("hides the composer on an ended session and says why", async () => {
    // 给一个发不出去的输入框,用户会打完一段话才发现。
    mocked.sessions.mockResolvedValue({ items: [ENDED], counts: {}, total: 1 });
    mocked.session.mockResolvedValue(ENDED);
    render(<Chat />);

    expect(await screen.findByText(/这个会话已结束,不能再发消息/)).toBeInTheDocument();
    expect(screen.queryByLabelText("输入消息")).toBeNull();
  });

  it("keeps the composer on a suspended session — that one can be resumed", async () => {
    // **已挂起不算已结束。** 合并的话,可恢复的会话会被无故剥夺输入。
    const suspended = { ...SESSION, state: "suspended", suspend_reason: "idle" };
    mocked.sessions.mockResolvedValue({ items: [suspended], counts: {}, total: 1 });
    mocked.session.mockResolvedValue(suspended);
    render(<Chat />);

    expect(await screen.findByLabelText("输入消息")).toBeInTheDocument();
  });

  it("filtering to 已结束 jumps to the ended sessions and drops the current pick", async () => {
    // 独立的"↺ 会话回放"按钮已经拿掉,行为并进"已结束"筛选 chip——多做的一步是清空
    // 当前选中的会话,不然中栏停在一条已经不在筛选结果里的会话上。
    mocked.sessions.mockImplementation((opts?: { state?: string }) =>
      Promise.resolve(
        opts?.state === "ended"
          ? { items: [], counts: { active: 1, suspended: 2, ended: 16 }, total: 19 }
          : { items: [SESSION], counts: { active: 1, suspended: 2, ended: 16 }, total: 19 },
      ),
    );
    render(<Chat />);
    await screen.findByText("超时后由补偿任务回滚");

    await userEvent.click(await screen.findByText("已结束 16"));

    await waitFor(() => expect(mocked.sessions).toHaveBeenCalledWith({ state: "ended" }));
    expect(await screen.findByText(/还没有会话/)).toBeInTheDocument();
    expect(screen.queryByText("超时后由补偿任务回滚")).not.toBeInTheDocument();
  });
});
