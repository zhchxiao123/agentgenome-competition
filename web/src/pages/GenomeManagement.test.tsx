import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GenomeManagement } from "./GenomeManagement";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      projectMap: vi.fn(),
      projectMapVersions: vi.fn(),
      knowledgeStatus: vi.fn(),
      knowledgeInit: vi.fn(),
    },
  };
});

const { api, ApiError } = await import("../api/client");
const mapMock = vi.mocked(api.projectMap);
const versionsMock = vi.mocked(api.projectMapVersions);
const knowledgeMock = vi.mocked(api.knowledgeStatus);
const initMock = vi.mocked(api.knowledgeInit);

beforeEach(() => {
  mapMock.mockReset();
  versionsMock.mockReset();
  knowledgeMock.mockReset();
  initMock.mockReset();
  mapMock.mockResolvedValue({
    version: 1,
    updated_at: null,
    project_name: "mall",
    modules: [],
    interfaces: [],
  });
  versionsMock.mockResolvedValue({ items: [] });
  knowledgeMock.mockResolvedValue({ modules: [], review: [], no_cards: [], cards: [] });
});

async function openKnowledge() {
  render(<GenomeManagement />);
  await userEvent.click(screen.getByRole("button", { name: "知识状态" }));
}

describe("知识初始化入口", () => {
  it("launches knowledge init from the knowledge tab and shows the task id", async () => {
    // 常驻入口:此前按钮只活在建项目当次的引导页,离开那一页就没有任何地方能发起了。
    initMock.mockResolvedValue({ id: "gn-1" } as never);
    await openKnowledge();

    await userEvent.click(screen.getByRole("button", { name: "发起知识初始化" }));

    expect(initMock).toHaveBeenCalled();
    expect(await screen.findByText("gn-1")).toBeInTheDocument();
    expect(screen.getByText(/确认边界草案/)).toBeInTheDocument();
  });

  it("shows the 409 verbatim when one is already running", async () => {
    // 报错里带着在跑那个任务的 id——下一步是去答它,不是重试。
    initMock.mockRejectedValue(
      new ApiError(409, "已有一个未了结的知识初始化在跑: gn-9。先去回答它的闸门,或取消它。"),
    );
    await openKnowledge();

    await userEvent.click(screen.getByRole("button", { name: "发起知识初始化" }));

    expect(await screen.findByText(/gn-9/)).toBeInTheDocument();
  });

  it("offers the launcher right where the empty skeleton is discovered", async () => {
    // 空骨架的人正站在最需要这个动作的地方——不只说"该跑一次",把按钮递到手边。
    render(<GenomeManagement />);

    expect(await screen.findByText(/项目地图还是空骨架/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发起知识初始化" })).toBeInTheDocument();
  });
});

describe("GenomeManagement 知识状态", () => {
  it("shows each module's knowledge and flags a low confidence one", async () => {
    knowledgeMock.mockResolvedValue({
      modules: [
        { module_id: "order", features: 3, cards: 2, no_cards: 1, confidence: 0.3 },
        { module_id: "pay", features: 1, cards: 1, no_cards: 0, confidence: 0.9 },
      ],
      review: [],
      no_cards: [],
      cards: [],
    });

    await openKnowledge();

    expect(await screen.findAllByText("order")).not.toHaveLength(0);
    // 低置信度说的是"这里的认知可能不可靠",要显眼。
    expect(screen.getByText("0.3").closest(".t")).toHaveClass("warn");
    expect(screen.getByText("0.9").closest(".t")).toHaveClass("ok");
  });

  it("shows the tree as modules with their feature cards nested under them", async () => {
    // 两张平表说不出"这张卡片属于哪个模块"这件事——而知识树的形状本身就是认知的一部分。
    knowledgeMock.mockResolvedValue({
      modules: [{ module_id: "order", features: 1, cards: 1, no_cards: 0, confidence: 0.9 }],
      review: [],
      no_cards: [],
      cards: [{ module_id: "order", feature_id: "place", title: "下单", hits: 0, confidence: null }],
    });

    await openKnowledge();

    const tree = (await screen.findByText("知识树")).closest(".card");
    expect(within(tree as HTMLElement).getByText("place")).toBeInTheDocument();
  });

  it("lists every no-card declaration with its reason", async () => {
    // 带理由的声明也算完备,但不摆出来的话"不值得写"会变成偷懒的托词而没有人发现。
    knowledgeMock.mockResolvedValue({
      modules: [],
      review: [],
      no_cards: [{ module_id: "order", feature_id: "config", reason: "只是一份常量表" }],
      cards: [],
    });

    await openKnowledge();

    expect(await screen.findByText("只是一份常量表")).toBeInTheDocument();
  });

  it("shows how often each card was actually used", async () => {
    // 命中次数是自然选择的依据:长期零命中的卡片该被淘汰。
    knowledgeMock.mockResolvedValue({
      modules: [],
      review: [],
      no_cards: [],
      cards: [{ module_id: "order", feature_id: "place", title: "下单", hits: 42, confidence: null }],
    });

    await openKnowledge();

    expect(await screen.findByText("42")).toBeInTheDocument();
  });

  it("searches the cards through the server, not by filtering in the page", async () => {
    await openKnowledge();
    await screen.findByRole("heading", { name: "功能卡片" });

    await userEvent.type(screen.getByLabelText("检索功能卡片"), "下单");

    expect(knowledgeMock).toHaveBeenCalledWith("下单");
  });

  it("says the knowledge tree is broken instead of showing an empty page", async () => {
    // 「还没建知识」与「知识树读不出来」在空页面上是同一个样子,而后者要立刻修。
    knowledgeMock.mockRejectedValue(new ApiError(422, "project-map.yaml: 解析失败"));

    await openKnowledge();

    expect(await screen.findByText(/拉不到知识状态/)).toHaveTextContent("解析失败");
  });

  it("keeps a separate history for each layer of the genome", async () => {
    // 只给知识那一层的话,"规则是什么时候被谁改成这样的"在界面上无解——而规则层恰恰是
    // 唯一能大范围改变系统行为的杠杆。
    mapMock.mockResolvedValue({
      version: 1,
      updated_at: null,
      project_name: "mall",
      modules: [{ id: "order", path: "order/", summary: "", depends_on: [] }],
      interfaces: [],
    });
    render(<GenomeManagement />);
    await screen.findByLabelText("看哪一层的历史");

    await userEvent.selectOptions(screen.getByLabelText("看哪一层的历史"), "rules");

    expect(versionsMock).toHaveBeenLastCalledWith("rules");
  });

  it("points a knowledge version back at the task that caused it", async () => {
    // 版本历史挂在依赖图那一屏下面,而那一屏在项目地图是空骨架时不渲染。
    mapMock.mockResolvedValue({
      version: 1,
      updated_at: null,
      project_name: "mall",
      modules: [{ id: "order", path: "order/", summary: "", depends_on: [] }],
      interfaces: [],
    });
    versionsMock.mockResolvedValue({
      items: [
        {
          rev: "abc12345",
          at: "2026-01-01T00:00:00+00:00",
          author: "AgentGenome",
          subject: "docs: 更新认知",
          source_task_id: "ag-0007",
        },
      ],
    });

    render(<GenomeManagement />);

    expect(await screen.findByText("ag-0007")).toBeInTheDocument();
  });
});
