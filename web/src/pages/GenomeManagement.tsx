/**
 * 基因组管理:把黑盒打开。
 *
 * **依赖图用轻量自绘 SVG,不接 AntV/G6。** 几十模块的规模一张分层列表 + 详情抽屉就够看清
 * 依赖关系,重图形库换来的交互收益在这个规模下配不上它的体积与学习成本——这是一处有意的
 * 简化,记在 `.scratch/INDEX.md` 的遗留账里。
 *
 * **规则编辑走 JSON 逃生舱,不是四种专用表单。** PRD 自己也承认"总会有表单没覆盖到的表达",
 * 需要留一个高级模式;这里直接从那个高级模式起步——校验与"提交生成 PR 不直接写"这条硬约束
 * 一样不少,只是没有为每种规则形态各画一份图形化表单。
 */
import { useEffect, useState } from "react";
import {
  ApiError,
  api,
  type KnowledgeStatus,
  type LessonCardResponse,
  type ProjectMapResponse,
  type ProjectMapVersionItem,
  type RuleSetResponse,
  type ProcedureStatsList,
} from "../api/client";
import { LogViewer } from "../ui/LogViewer";
import { Card, Empty, Note, Tag } from "../ui/kit";

const TABS = [
  ["map", "依赖图"],
  ["knowledge", "知识状态"],
  ["lessons", "经验卡片库"],
  ["rules", "架构规则"],
  ["procedures", "Procedure 注册表"],
] as const;

type GenomeTab = (typeof TABS)[number][0];

function isGenomeTab(value: string | undefined): value is GenomeTab {
  return TABS.some(([id]) => id === value);
}

export function GenomeManagement({ tab: requestedTab, onTab }: { tab?: string; onTab?: (tab: GenomeTab) => void } = {}) {
  const [localTab, setLocalTab] = useState<GenomeTab>("map");
  const tab = isGenomeTab(requestedTab) ? requestedTab : localTab;
  const selectTab = (next: GenomeTab) => {
    if (onTab) onTab(next);
    else setLocalTab(next);
  };

  return (
    <>
      <h1>基因组管理</h1>
      <div className="sub">项目地图、功能卡片、经验卡片、规则与 Procedure —— 系统学到了什么,一眼可见</div>

      <div className="tabs">
        {TABS.map(([id, label]) => (
          <button key={id} className={tab === id ? "on" : ""} onClick={() => selectTab(id)}>
            {label}
          </button>
        ))}
      </div>

      {tab === "map" && <ProjectMapView />}
      {tab === "knowledge" && <KnowledgeView />}
      {tab === "lessons" && <LessonLibrary />}
      {tab === "rules" && <RuleEditor />}
      {tab === "procedures" && <ProcedureStats />}
    </>
  );
}

// --- 依赖图 -------------------------------------------------------------------

function KnowledgeInitLauncher() {
  /**
   * 知识初始化的常驻入口。此前它只活在建项目当次的引导页里,离开那一页入口就没了——
   * 而"发起初始化"是随时可能需要的动作(接入完成后、认知需要重来时),不是建项目的
   * 附属品。重复发起由服务端拒绝(409 带着在跑那个任务的 id),这里原样显示。
   */
  const [started, setStarted] = useState("");
  const [error, setError] = useState("");

  const launch = () => {
    api
      .knowledgeInit()
      .then((task) => {
        setStarted(task.id);
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
  };

  if (started) {
    return (
      <Note>
        知识初始化已发起(任务 <span className="mono">{started}</span>)——
        去「任务中心」的基因组任务看板确认边界草案,确认后由架构员工逐模块深读。
      </Note>
    );
  }
  return (
    <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
      <button className="btn pri" onClick={launch}>
        发起知识初始化
      </button>
      <span className="m">扫描仓库 → 人确认模块边界 → 逐模块深读。已有一个在跑会被拒并指向它。</span>
      {error && <Note tone="warn">{error}</Note>}
    </div>
  );
}

function ProjectMapView() {
  const [map, setMap] = useState<ProjectMapResponse | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .projectMap()
      .then((found) => {
        setMap(found);
        setSelected((current) => current ?? found.modules[0]?.id ?? null);
      })
      .catch((e: ApiError) => setError(e.detail));
  }, []);

  if (error) return <Note tone="warn">{error}</Note>;
  if (!map) return <Empty>加载中…</Empty>;
  if (map.modules.length === 0) {
    // 不只说"该跑一次",把按钮递到手边——空骨架的人正站在最需要这个动作的地方。
    return (
      <>
        <Empty>项目地图还是空骨架 —— 发起知识初始化,让架构员工补齐认知</Empty>
        <KnowledgeInitLauncher />
      </>
    );
  }

  const module = map.modules.find((item) => item.id === selected) ?? null;
  const dependents = map.modules.filter((item) => (item.depends_on ?? []).includes(selected ?? ""));
  const interfaces = map.interfaces.filter(
    (item) => item.provider === selected || (item.consumers ?? []).includes(selected ?? ""),
  );

  return (
    <div className="grid" style={{ gridTemplateColumns: "260px 1fr", alignItems: "start" }}>
      <Card
        title="模块"
        extra={
          <span className="mono">
            v{map.version} · {map.updated_at ? new Date(map.updated_at).toLocaleDateString() : "未知时间"}
          </span>
        }
      >
        {map.modules.map((item) => (
          <div
            key={item.id}
            className="tk"
            onClick={() => setSelected(item.id)}
            style={selected === item.id ? { borderColor: "var(--pri)", background: "var(--pri-bg)" } : undefined}
          >
            <b>{item.id}</b>
            <div className="m">{item.summary || "(还没有认知)"}</div>
            {item.confidence != null && <Tag tone={item.confidence >= 0.7 ? "ok" : "warn"}>置信 {item.confidence}</Tag>}
          </div>
        ))}
      </Card>

      {module ? (
        <div>
          <Card title={`${module.id} · 功能卡片`}>
            <div className="kv"><span className="k">路径</span><span className="v mono">{module.path}</span></div>
            <div className="kv"><span className="k">语言</span><span className="v">{module.lang ?? "—"}</span></div>
            <div className="kv"><span className="k">置信度</span><span className="v">{module.confidence ?? "—"}</span></div>
            <div className="kv"><span className="k">文档</span><span className="v mono">{module.doc ?? "—"}</span></div>
            <p style={{ marginTop: 10, color: "var(--ink2)" }}>{module.summary || "(架构员工还没写摘要)"}</p>
          </Card>

          <Card title="依赖关系" extra={<span className="m">依赖它的模块最近改动多,是热点</span>}>
            <div className="kv">
              <span className="k">依赖谁</span>
              <span className="v">
                {(module.depends_on ?? []).length ? (module.depends_on ?? []).map((id) => <Tag key={id}>{id}</Tag>) : "(无)"}
              </span>
            </div>
            <div className="kv">
              <span className="k">被谁依赖</span>
              <span className="v">
                {dependents.length ? dependents.map((item) => <Tag key={item.id}>{item.id}</Tag>) : "(无)"}
              </span>
            </div>
          </Card>

          <Card title="跨模块契约">
            {interfaces.length === 0 ? (
              <Empty>这个模块没有登记跨模块契约</Empty>
            ) : (
              <table>
                <thead>
                  <tr><th>契约</th><th>类型</th><th>provider → consumers</th></tr>
                </thead>
                <tbody>
                  {interfaces.map((item) => (
                    <tr key={item.id}>
                      <td className="mono">{item.id}</td>
                      <td>{item.kind}</td>
                      <td>{item.provider} → {(item.consumers ?? []).join(", ") || "(无)"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </div>
      ) : (
        <Empty>选一个模块</Empty>
      )}

      <div style={{ gridColumn: "1 / -1" }}>
        <VersionHistory />
      </div>
    </div>
  );
}

/** 基因组三层各自的历史版本与版本间的 diff —— 演进可回溯。 */
const ASSETS = [
  ["knowledge", "知识"],
  ["rules", "规则"],
  ["procedures", "工序"],
] as const;

function VersionHistory() {
  // **三层各查各的。** 只给知识那一层的话,"规则是什么时候被谁改成这样的"在界面上无解
  // ——而规则层恰恰是唯一能大范围改变系统行为的杠杆。
  const [asset, setAsset] = useState<(typeof ASSETS)[number][0]>("knowledge");
  const [versions, setVersions] = useState<ProjectMapVersionItem[]>([]);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [diff, setDiff] = useState("");
  const [error, setError] = useState("");
  // 拉版本列表失败与"这个项目地图还没有历史版本"曾经是同一条路径(`.catch(() =>
  // setVersions([]))`),而下面 `if (versions.length === 0) return null` 会让两者
  // 都表现成"这块卡片直接消失"——比显示错的空态还糟,连"这里本该有东西"都看不出来。
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    api
      .projectMapVersions(asset)
      .then((found) => {
        setVersions(found.items);
        setLoadError("");
        const [latest, previous] = found.items;
        if (latest && previous) {
          setFrom(previous.rev);
          setTo(latest.rev);
        }
      })
      .catch((e: ApiError) => setLoadError(e.detail));
  }, [asset]);

  const showDiff = () => {
    if (!from || !to) return;
    api
      .projectMapDiff(from, to, asset)
      .then((body) => setDiff(body.diff))
      .catch((e: ApiError) => setError(e.detail));
  };

  if (loadError) return <Note tone="warn">拉不到版本历史:{loadError}</Note>;

  return (
    <Card
      title="版本历史"
      extra={
        <select
          aria-label="看哪一层的历史"
          value={asset}
          onChange={(event) => setAsset(event.target.value as (typeof ASSETS)[number][0])}
        >
          {ASSETS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      }
    >
      {versions.length === 0 && <Empty>这一层还没有改动记录。</Empty>}
      <table>
        <thead><tr><th></th><th>提交</th><th>时间</th><th>作者</th><th>说明</th><th>来源任务</th></tr></thead>
        <tbody>
          {versions.map((item) => (
            <tr key={item.rev}>
              <td className="mono">
                <input type="radio" name="from" checked={from === item.rev} onChange={() => setFrom(item.rev)} title="起点" />{" "}
                <input type="radio" name="to" checked={to === item.rev} onChange={() => setTo(item.rev)} title="终点" />
              </td>
              <td className="mono">{item.rev.slice(0, 8)}</td>
              <td className="mono">{new Date(item.at).toLocaleString()}</td>
              <td>{item.author}</td>
              <td>{item.subject}</td>
              {/* 「这条认知从哪次经验来的」——从提交正文里读现有的任务号,不另存一份
                  关联表:那份表会与 git 历史分叉,而分叉之后没法判断哪份是对的。 */}
              <td className="mono">
                {item.source_task_id ? (
                  <a href={`#/tasks/${item.source_task_id}`}>{item.source_task_id}</a>
                ) : (
                  "—"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button className="btn sm pri" style={{ marginTop: 10 }} onClick={showDiff}>
        查看 {from.slice(0, 8) || "?"} → {to.slice(0, 8) || "?"} 的 diff
      </button>
      {error && <Note tone="warn">{error}</Note>}
      {diff && (
        <div style={{ marginTop: 10 }}>
          <LogViewer text={diff} />
        </div>
      )}
    </Card>
  );
}

// --- 知识状态 -------------------------------------------------------------------

/**
 * 每模块的知识状态、要复核的、声明了不需要卡片的、以及每张卡片被用上了几次。
 *
 * **「无需卡片」的声明要摆出来。** 带理由的声明也算完备(ADR-0003),但不摆出来的话,
 * "不值得写"会变成偷懒的托词而没有人发现——而这一页恰恰是唯一能扫一眼看出来的地方。
 */
function KnowledgeView() {
  const [status, setStatus] = useState<KnowledgeStatus | null>(null);
  const [q, setQ] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .knowledgeStatus(q)
      .then((found) => {
        setStatus(found);
        setError("");
      })
      // 「还没建知识」与「知识树读不出来」在空页面上是同一个样子,而后者要立刻修。
      .catch((e: ApiError) => setError(e.detail));
  }, [q]);

  if (error) return <Note tone="warn">拉不到知识状态:{error}</Note>;
  if (!status) return <Empty>加载中…</Empty>;

  return (
    <>
      <Card title="知识初始化">
        <KnowledgeInitLauncher />
      </Card>
      <Card title="每模块的认知">
        <table>
          <thead>
            <tr><th>模块</th><th>功能点</th><th>功能卡片</th><th>无需功能卡片</th><th>置信度</th></tr>
          </thead>
          <tbody>
            {(status.modules ?? []).map((row) => (
              <tr key={row.module_id}>
                <td className="mono">{row.module_id}</td>
                <td>{row.features}</td>
                <td>{row.cards}</td>
                <td>{row.no_cards}</td>
                <td>
                  {row.confidence == null ? (
                    "—"
                  ) : (
                    // 低置信度要显眼:它说的是"这里的认知可能不可靠"。
                    <Tag tone={row.confidence < 0.5 ? "warn" : "ok"}>{row.confidence}</Tag>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card title="要人复核的" extra={<span className="m">置信度低的先看</span>}>
        {(status.review ?? []).length === 0 ? (
          <Empty>没有低置信度的功能卡片。</Empty>
        ) : (
          <ul>
            {(status.review ?? []).map((row) => (
              <li key={`${row.module_id}/${row.feature_id}`}>
                <span className="mono">{row.module_id}/{row.feature_id}</span> {row.title}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="声明了「无需功能卡片」的" extra={<span className="m">带理由才算数</span>}>
        {(status.no_cards ?? []).length === 0 ? (
          <Empty>没有这类声明。</Empty>
        ) : (
          <table>
            <tbody>
              {(status.no_cards ?? []).map((row) => (
                <tr key={`${row.module_id}/${row.feature_id}`}>
                  <td className="mono">{row.module_id}/{row.feature_id}</td>
                  <td>{row.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="知识树" extra={<span className="m">模块 → 功能点,与代码结构同构</span>}>
        {(status.modules ?? []).length === 0 ? (
          <Empty>还没有模块。</Empty>
        ) : (
          <ul className="tree">
            {(status.modules ?? []).map((module) => {
              const owned = (status.cards ?? []).filter((card) => card.module_id === module.module_id);
              return (
                <li key={module.module_id}>
                  <b className="mono">{module.module_id}</b>
                  {/* 一个模块建了功能点却一张卡片都没有,值得一眼看出来。 */}
                  {owned.length === 0 ? (
                    <span className="m"> (还没有功能卡片)</span>
                  ) : (
                    <ul>
                      {owned.map((card) => (
                        <li key={card.feature_id} className="m">
                          <span className="mono">{card.feature_id}</span> {card.title}
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <Card
        title="功能卡片"
        extra={
          <input
            aria-label="检索功能卡片"
            placeholder="按 id 或标题检索"
            value={q}
            onChange={(event) => setQ(event.target.value)}
          />
        }
      >
        {(status.cards ?? []).length === 0 ? (
          <Empty>没有匹配的功能卡片。</Empty>
        ) : (
          <table>
            <thead><tr><th>模块</th><th>功能点</th><th>标题</th><th>命中</th></tr></thead>
            <tbody>
              {(status.cards ?? []).map((row) => (
                <tr key={`${row.module_id}/${row.feature_id}`}>
                  <td className="mono">{row.module_id}</td>
                  <td className="mono">{row.feature_id}</td>
                  <td>{row.title}</td>
                  {/* 命中次数是自然选择的依据:长期零命中的卡片该被淘汰。 */}
                  <td>{row.hits}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}

// --- 知识卡片库 -----------------------------------------------------------------

function LessonLibrary() {
  const [items, setItems] = useState<LessonCardResponse[]>([]);
  const [q, setQ] = useState("");
  const [module, setModule] = useState("");
  const [minHits, setMinHits] = useState(0);
  const [status, setStatus] = useState<"active" | "archived" | "all">("active");
  const [sort, setSort] = useState<"hits" | "created" | "confidence">("hits");
  const [showAdd, setShowAdd] = useState(false);
  const [error, setError] = useState("");

  const reload = () => {
    api
      .lessons({ q, module, min_hits: String(minHits), status, sort })
      .then((found) => setItems(found.items))
      .catch((e: ApiError) => setError(e.detail));
  };

  useEffect(reload, [q, module, minHits, status, sort]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Card
      title={`经验卡片库(${items.length})`}
      extra={<button className="btn sm pri" onClick={() => setShowAdd((v) => !v)}>{showAdd ? "收起" : "+ 手工添加"}</button>}
    >
      <div className="grid g3" style={{ marginBottom: 8 }}>
        <input placeholder="按标题/结论检索" value={q} onChange={(e) => setQ(e.target.value)} />
        <input placeholder="按适用模块过滤,如 order-service" value={module} onChange={(e) => setModule(e.target.value)} />
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <span className="m" style={{ whiteSpace: "nowrap" }}>命中数 ≥</span>
          <input type="number" min={0} value={minHits} onChange={(e) => setMinHits(Number(e.target.value) || 0)} />
        </div>
      </div>
      <div className="grid g2" style={{ marginBottom: 12 }}>
        <select value={status} onChange={(e) => setStatus(e.target.value as typeof status)}>
          <option value="active">活跃</option>
          <option value="archived">归档区</option>
          <option value="all">全部</option>
        </select>
        <select value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
          <option value="hits">按命中数排序</option>
          <option value="created">按创建时间排序</option>
          <option value="confidence">按置信度排序</option>
        </select>
      </div>

      {showAdd && <AddLessonForm onDone={() => { setShowAdd(false); reload(); }} />}
      {error && <Note tone="warn">{error}</Note>}

      {items.length === 0 ? (
        <Empty>没有匹配的经验卡片</Empty>
      ) : (
        <table>
          <thead>
            <tr><th>标题</th><th>适用模块</th><th>命中</th><th>置信度</th><th>来源</th><th>状态</th><th></th></tr>
          </thead>
          <tbody>
            {items.map((card) => (
              <tr key={card.id}>
                <td><b>{card.title}</b><div className="m mono">{card.id}</div></td>
                <td>{card.modules.join(", ") || card.scenario || "(通用)"}</td>
                {/* 命中次数是这套自进化机制唯一对外可见的"这条经验到底有没有用"。 */}
                <td><b>{card.hits}</b></td>
                <td>{card.confidence}</td>
                <td>{card.created_from}</td>
                <td>{card.archived ? <Tag tone="mute">已归档</Tag> : <Tag tone="ok">活跃</Tag>}</td>
                <td>
                  {card.archived ? (
                    <button className="btn sm" onClick={() => api.restoreLesson(card.id).then(reload)}>恢复</button>
                  ) : (
                    <button className="btn sm" onClick={() => api.deprecateLesson(card.id).then(reload)}>标记过时</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function AddLessonForm({ onDone }: { onDone: () => void }) {
  const [title, setTitle] = useState("");
  const [modules, setModules] = useState("");
  const [conclusion, setConclusion] = useState("");
  const [evidenceTask, setEvidenceTask] = useState("");
  const [evidencePath, setEvidencePath] = useState("");
  const [error, setError] = useState("");

  const submit = () => {
    api
      .addLesson({
        title,
        modules: modules.split(",").map((s) => s.trim()).filter(Boolean),
        scenario: "",
        conclusion,
        evidence: evidenceTask && evidencePath ? [{ task_id: evidenceTask, path: evidencePath, note: "" }] : [],
        confidence: 0.6,
      })
      .then(onDone)
      .catch((e: ApiError) => setError(e.detail));
  };

  return (
    <div className="card" style={{ background: "var(--bg)", marginBottom: 12 }}>
      <label>标题 <span className="req">*</span></label>
      <input value={title} onChange={(e) => setTitle(e.target.value)} />
      <label>适用模块 <span className="hint">· 逗号分隔</span></label>
      <input value={modules} onChange={(e) => setModules(e.target.value)} placeholder="order-service, inventory-service" />
      <label>结论 <span className="req">*</span></label>
      <textarea value={conclusion} onChange={(e) => setConclusion(e.target.value)} />
      <div className="grid g2">
        <div>
          <label>证据 · 任务编号 <span className="req">*</span></label>
          <input value={evidenceTask} onChange={(e) => setEvidenceTask(e.target.value)} placeholder="ag-20260101-001" />
        </div>
        <div>
          <label>证据 · 产物路径 <span className="req">*</span></label>
          <input value={evidencePath} onChange={(e) => setEvidencePath(e.target.value)} placeholder="logs/events.jsonl" />
        </div>
      </div>
      <Note>没有证据链接的一律拒收 —— 这是防污染的第一道,人工添加同样要过。</Note>
      {error && <div className="err on">{error}</div>}
      <button className="btn pri" style={{ marginTop: 10 }} onClick={submit}>提交</button>
    </div>
  );
}

// --- 规则 ----------------------------------------------------------------------

const SECTIONS = [
  ["architecture", "架构规则"],
  ["protected", "受保护路径"],
  ["impact", "影响规则"],
] as const;

function RuleEditor() {
  const [rules, setRules] = useState<RuleSetResponse | null>(null);
  const [section, setSection] = useState<(typeof SECTIONS)[number][0]>("impact");
  const [draft, setDraft] = useState("");
  const [description, setDescription] = useState("");
  const [actor, setActor] = useState("");
  const [result, setResult] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api.rules().then(setRules).catch((e: ApiError) => setError(e.detail));
  }, []);

  useEffect(() => {
    if (rules) setDraft(JSON.stringify(rules[section], null, 2));
  }, [rules, section]);

  const submit = () => {
    setError("");
    setResult("");
    let payload: unknown;
    try {
      payload = JSON.parse(draft);
    } catch {
      setError("不是合法的 JSON");
      return;
    }
    api
      // 从这个页面改规则是人主动发起的，没有来源任务；有来源任务的提案由任务流程自己带。
      .proposeRuleChange({
        section,
        payload: payload as Record<string, unknown>,
        description,
        actor,
        source_task_id: "",
      })
      .then((pr) => setResult(`已提交提案 PR #${pr.number}(${pr.head} → ${pr.base})—— 不会自动合并`))
      .catch((e: ApiError) => setError(e.detail));
  };

  if (!rules) return error ? <Note tone="warn">{error}</Note> : <Empty>加载中…</Empty>;

  return (
    <Card title="架构规则">
      <div className="chips" style={{ marginBottom: 12 }}>
        {SECTIONS.map(([id, label]) => (
          <span key={id} className={section === id ? "on" : ""} onClick={() => setSection(id)}>
            {label}
          </span>
        ))}
      </div>

      <Note>
        这是高级(JSON)编辑模式 —— 总会有图形化表单没覆盖到的表达。<b>提交生成一个待审批的规则变更 PR,不会直接写入。</b>
      </Note>

      <label>{SECTIONS.find(([id]) => id === section)?.[1]} · 当前生效值</label>
      <textarea value={draft} onChange={(e) => setDraft(e.target.value)} style={{ minHeight: 220, fontFamily: "monospace" }} />

      <div className="grid g2">
        <div>
          <label>变更说明</label>
          <input value={description} onChange={(e) => setDescription(e.target.value)} />
        </div>
        <div>
          <label>提出人 <span className="req">*</span></label>
          <input value={actor} onChange={(e) => setActor(e.target.value)} />
        </div>
      </div>

      {error && <div className="err on">{error}</div>}
      {result && <Note>{result}</Note>}
      <button className="btn pri" style={{ marginTop: 10 }} onClick={submit} disabled={!actor.trim()}>
        提交提案
      </button>
    </Card>
  );
}

// --- Procedure 注册表 --------------------------------------------------------------

function ProcedureStats() {
  const [stats, setStats] = useState<ProcedureStatsList | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .procedureStats()
      .then(setStats)
      .catch((e: ApiError) => setError(e.detail));
  }, []);

  if (error) return <Empty>拉不到 Procedure 注册表:{error}</Empty>;
  if (!stats) return <Empty>加载中…</Empty>;
  if (stats.items.length === 0) return <Empty>还没有注册任何 Procedure</Empty>;

  return (
    <Card title="Procedure 注册表">
      <table>
        <thead>
          <tr><th>Procedure</th><th>版本</th><th>来源</th><th>类型</th><th>可用</th><th>调用次数</th><th>失败率</th></tr>
        </thead>
        <tbody>
          {stats.items.map((item) => (
            <tr key={`${item.id}@${item.version}`}>
              <td className="mono">{item.id}</td>
              <td>{item.version}</td>
              <td><Tag tone={item.source === "project" ? "pri" : "pur"}>{item.source}</Tag></td>
              <td>{item.kind}</td>
              <td>{item.available ? <Tag tone="ok">可用</Tag> : <Tag tone="bad">不可用</Tag>}</td>
              <td>{item.call_count}</td>
              <td>{item.call_count ? `${(item.failure_rate * 100).toFixed(0)}%` : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}
