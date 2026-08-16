/**
 * 员工管理页:七类员工各出场几次、花了多少,以及质量线三个旋钮拧在哪一档——**并且拧得动**。
 *
 * **出场为 0 的也列出来。** "这个项目根本没用过对抗"与"页面上没有这一行"是完全不同的两件
 * 事,而后者会让人以为那个角色不存在——于是"要不要把质量投入调高"这个问题连提都提不出来。
 *
 * **三个旋钮排在一起是这一页的事,不是配置层的事。** 评审那一档的配置住在 `topology.critique`,
 * 把它搬进质量线配置节是一次破坏性迁移,换来的只是"排在一起好看"。这一页就是那个"一起看"。
 *
 * **表单的数据源是 `settings()`,不是 `roster()` 的 dial。** dial 是给人看的档位摘要:
 * 精化环那一项的值是合成出来的开/关,阈值与确认名单在里面根本不存在。拿它当数据源,等于让
 * 前端从一个形状读、往另一个形状写,而两者第一次漂移时没有任何东西会报警。
 */
import { useEffect, useState } from "react";
import {
  ApiError,
  api,
  type RosterReport,
  type SettingsView,
  type WorkerStatusView,
} from "../api/client";
import { ContainerRuntimeCard } from "../components/ContainerRuntimeCard";
import { EmployeeRuntimePicker } from "../components/EmployeeRuntimePicker";
import { WorkerPlanCard } from "../components/WorkerPlanCard";
import { WorkerRecycleActions } from "../components/WorkerRecycleActions";
import { WorkerStatusCell } from "../components/WorkerStatusCell";
import { Card, Empty, Note } from "../ui/kit";

// 配置那两段的形状**从生成类型里取**,不在这里手抄一份:抄一份的话,后端加字段而这里
// 没跟上时,保存会把那个字段连同它的默认值一起写回去,而没有任何东西会报错。
type QualityLine = SettingsView["quality_line"];
type Topology = SettingsView["topology"];
type Critique = Topology["critique"];

/** 容器运行时那一段在配置里的键。**一处**——散成字面量的话,改名时漏掉的那处不会报错。 */
const AGENTTEAMS = "agentteams";

export function Roster() {
  const [report, setReport] = useState<RosterReport | null>(null);
  const [settings, setSettings] = useState<SettingsView | null>(null);
  const [error, setError] = useState("");
  // 草稿与已生效的配置分开存。**写失败时草稿回到已生效那一份**——留一个视觉上已生效的
  // 档位,是在界面上伪造一个后端并不认的状态。
  const [line, setLine] = useState<QualityLine | null>(null);
  const [topology, setTopology] = useState<Topology | null>(null);
  const [saving, setSaving] = useState("");
  const [failed, setFailed] = useState("");
  // 正在编辑的那一行:员工、要挪到哪一档、这些活归谁。**转 manual 之前先问归谁**——
  // 没有主人的待办不会被任何人看到,只会在三窗口超时之后升级,一次静默的死循环。
  const [draft, setDraft] = useState<{ employee: string; rung: string; assignee: string } | null>(
    null,
  );
  // 容器状态按员工 id 索引。**服务端只回容器员工**,所以查不到就是"跑在本地",那一格
  // 是空的——写「未供应」会让人以为该去供应它。
  const [workers, setWorkers] = useState<Map<string, WorkerStatusView>>(new Map());
  // 整条状态请求为什么挂了。空串表示没挂。
  const [workersFailed, setWorkersFailed] = useState("");

  /** 重读容器状态。**每次去问平台**——Worker 重建会换房间,缓存的那个会静默过期。 */
  const loadWorkers = () =>
    api
      .workerStatuses()
      .then((view) => {
        setWorkers(new Map(view.items.map((item) => [item.employee_id, item])));
        setWorkersFailed("");
      })
      .catch((e: ApiError) => {
        // **说清为什么是空的。** 整条请求挂掉(比如服务端缺凭证,拿不到供应通道)时
        // 一片空白读起来像"一个都没供应",而那会诱人去点一次注定失败的供应。
        setWorkers(new Map());
        setWorkersFailed(e.detail);
      });

  useEffect(() => {
    api
      .roster()
      .then(setReport)
      .catch((e: ApiError) => setError(e.detail));
    api
      .settings()
      .then(load)
      // 读不到配置不该让整页打不开:出场与花费仍然是这一页的主要内容。
      .catch(() => setSettings(null));
    loadWorkers();
  }, []);

  const notes = new Map((report?.dials ?? []).map((dial) => [dial.key, dial.note]));
  const editable = settings?.can_edit ?? false;

  /** 把读回来的配置铺进草稿。**一处**,读与写后重读都走它。 */
  function load(found: SettingsView) {
    setSettings(found);
    setLine(found.quality_line);
    setTopology(found.topology);
  }

  const save = (section: "quality_line" | "topology", value: unknown, label: string) => {
    if (!settings) return;
    setSaving(section);
    setFailed("");
    api
      .updateSettings(section, value)
      .then(() => {
        setSettings({ ...settings, [section]: value } as SettingsView);
        // 出场次数会随配置变化,拧完顺手把花名册也拉一遍。
        api.roster().then(setReport).catch(() => undefined);
      })
      .catch((e: ApiError) => {
        setFailed(`${label}没改成:${e.detail}`);
        load(settings);
      })
      .finally(() => setSaving(""));
  };

  /**
   * 把一个员工挪到另一档。**服务端管分派**:三档住在两个存储上,前端拼一遍的话,拼错
   * 的那一次会造出"human 却又在确认名单里"这种谁都解释不了的状态。
   */
  const moveRung = (employee: string, rung: string, who = "") => {
    setSaving(employee);
    setFailed("");
    api
      .setExecution(employee, rung, who)
      .then(() => {
        setDraft(null);
        // **必须把配置也重读一遍。** 挪档位改的是确认名单,而本地那份 topology 还停在
        // 旧名单上;不重读的话,接下来一次「保存精化环」会把旧名单原样写回去,档位悄悄
        // 回退而两次保存都提示成功。
        return Promise.all([api.roster().then(setReport), api.settings().then(load)]);
      })
      .catch((e: ApiError) => setFailed(`${employee} 的执行档位没改成:${e.detail}`))
      .finally(() => setSaving(""));
  };

  /** 改精化环的一个字段。五处一样的展开收在这里,加字段时只有一处要改。 */
  const patchCritique = (change: Partial<Critique>) => {
    if (!topology) return;
    setTopology({ ...topology, critique: { ...topology.critique, ...change } as Critique });
  };

  /** 选了一档。**manual 先问归谁**,别的当场就写。 */
  const pickRung = (employee: string, rung: string, current: string) => {
    if (rung === "manual") {
      setDraft({ employee, rung, assignee: current });
      return;
    }
    setDraft(null);
    moveRung(employee, rung);
  };

  if (error) return <Empty>拉不到花名册:{error}</Empty>;
  if (!report) return <Empty>加载中…</Empty>;

  const total = report.employees.reduce((sum, item) => sum + item.tokens, 0);
  const critique = topology?.critique;
  // **没配容器运行时的工作区不出现这一列**(PRD 33)。一列永远空着的表格,读起来像
  // "查不到",而这里是"这个项目根本没启用容器"——两者要人做的事完全不同。
  const containers = AGENTTEAMS in (settings?.runtime?.runtimes ?? {});

  return (
    <>
      <h1>员工管理</h1>
      <div className="sub">这个项目的质量投入拧到哪一档 —— 看得见,也拧得动</div>

      {failed && <div className="err on">{failed}</div>}

      {/* 容器运行时:配置 + 就绪检查。没配过时它自己收起来(PRD 33)。 */}
      {settings && (
        <ContainerRuntimeCard
          runtime={settings.runtime}
          canEdit={editable}
          onSaved={() => api.settings().then(load).catch(() => undefined)}
        />
      )}

      {/* 状态整条读不到时说清原因:一片空白读起来像"一个都没供应"。 */}
      {containers && workersFailed && (
        <Note tone="warn">读不到容器状态:{workersFailed}</Note>
      )}

      {/* 供应计划:只读预演,看过才给执行入口(PRD 33)。没配容器运行时就没有这回事。 */}
      {containers && <WorkerPlanCard onProvisioned={loadWorkers} />}

      <Card title="质量线" extra={<span className="m">三个旋钮</span>}>
        {!settings && <Note tone="warn">读不到配置,旋钮这次只能看不能拧。</Note>}
        {settings && !editable && (
          <Note tone="warn">你没有改配置的权限,旋钮是只读的。</Note>
        )}

        {line && (
          <div className="grid g2" style={{ gap: 14 }}>
            <div>
              <label htmlFor="tester">测试分工</label>
              <select
                id="tester"
                value={line.tester}
                disabled={!editable}
                onChange={(e) => setLine({ ...line, tester: e.target.value as QualityLine["tester"] })}
              >
                <option value="dev">dev · 开发员工顺手写</option>
                <option value="dedicated">dedicated · 测试员工专职出题</option>
                <option value="risk-based">risk-based · 命中受保护路径才专职</option>
              </select>
              <div className="hint">{notes.get("tester")}</div>
            </div>
            <div>
              <label htmlFor="adversary">对抗 QA</label>
              <select
                id="adversary"
                value={line.adversary}
                disabled={!editable}
                onChange={(e) =>
                  setLine({ ...line, adversary: e.target.value as QualityLine["adversary"] })
                }
              >
                <option value="off">off · 不上场</option>
                <option value="protected-hit">protected-hit · 命中受保护路径才上</option>
                <option value="always">always · 每次都上</option>
              </select>
              <div className="hint">{notes.get("adversary")}</div>
            </div>
          </div>
        )}
        {line && (
          <button
            className="btn"
            style={{ marginTop: 12 }}
            disabled={!editable || saving === "quality_line"}
            onClick={() => save("quality_line", line, "质量线")}
          >
            {saving === "quality_line" ? "保存中…" : "保存质量线"}
          </button>
        )}
      </Card>

      {critique && topology && (
        // **按段保存,不做整页 Save。** 一次写入覆盖多段,会把别人这期间改的另一段
        // 一起盖回旧值,而设置层那把互斥锁挡不住这个。
        <Card title="评审精化环" extra={<span className="m">topology.critique</span>}>
          <label htmlFor="critique">评审精化环</label>
          <select
            id="critique"
            value={critique.enabled ? "on" : "off"}
            disabled={!editable}
            onChange={(e) =>
              patchCritique({ enabled: e.target.value === "on" })
            }
          >
            <option value="off">off · 不进环</option>
            <option value="on">on · 开发之后加一轮只批不改的评审</option>
          </select>
          {/* 这一档的解释与另外两个旋钮同源(花名册的 dial),别在这里另写一份。 */}
          <div className="hint">{notes.get("reviewer")}</div>

          {/* 「开关」与「开到多深」是一个动作,不是两次跳转。 */}
          {critique.enabled && (
            <div className="grid g2" style={{ gap: 14, marginTop: 12 }}>
              <div>
                <label htmlFor="on_protected">命中受保护路径</label>
                <select
                  id="on_protected"
                  value={critique.on_protected ? "on" : "off"}
                  disabled={!editable}
                  onChange={(e) =>
                    patchCritique({ on_protected: e.target.value === "on" })
                  }
                >
                  <option value="on">on · 就进环</option>
                  <option value="off">off · 不因此进环</option>
                </select>
              </div>
              <div>
                <label htmlFor="min_modules">模块数达到就进环</label>
                <input
                  id="min_modules"
                  type="number"
                  value={critique.min_modules}
                  disabled={!editable}
                  onChange={(e) =>
                    patchCritique({ min_modules: Number(e.target.value) })
                  }
                />
              </div>
              <div>
                <label htmlFor="min_changed_files">改动文件数达到就进环</label>
                <input
                  id="min_changed_files"
                  type="number"
                  value={critique.min_changed_files}
                  disabled={!editable}
                  onChange={(e) =>
                    patchCritique({ min_changed_files: Number(e.target.value) })
                  }
                />
              </div>
              <div>
                <label htmlFor="max_rounds">轮次上限</label>
                <input
                  id="max_rounds"
                  type="number"
                  value={critique.max_rounds}
                  disabled={!editable}
                  onChange={(e) =>
                    patchCritique({ max_rounds: Number(e.target.value) })
                  }
                />
              </div>
              <div>
                <label htmlFor="budget_share">占任务预算的份额</label>
                <input
                  id="budget_share"
                  type="number"
                  step="0.05"
                  value={critique.budget_share}
                  disabled={!editable}
                  onChange={(e) =>
                    patchCritique({ budget_share: Number(e.target.value) })
                  }
                />
                {/* 归因不是封顶:轮次上限管得住次数,管不住单轮的花费。 */}
                <div className="hint">归因份额,不是硬上限</div>
              </div>
            </div>
          )}

          <button
            className="btn"
            style={{ marginTop: 12 }}
            disabled={!editable || saving === "topology"}
            onClick={() => save("topology", topology, "精化环")}
          >
            {saving === "topology" ? "保存中…" : "保存精化环"}
          </button>
        </Card>
      )}

      <Card
        title="花名册"
        extra={<span className="mono">合计 {total.toLocaleString()} tokens</span>}
      >
        {report.employees.length === 0 ? (
          <Empty>这个工作区还没有员工定义 —— 跑一次 `agctl roster migrate` 补齐</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>员工</th><th>执行档位</th>
                {containers && <th>容器状态</th>}
                <th>出场</th><th>tokens</th><th>定位</th>
              </tr>
            </thead>
            <tbody>
              {report.employees.map((item) => {
                const mine = draft?.employee === item.id ? draft : null;
                const becoming = mine?.rung === "manual" && item.execution !== "manual";
                return (
                <tr key={item.id}>
                  <td>
                    {item.name}
                    <div className="m mono">{item.id}</div>
                  </td>
                  <td>
                    {/* **一个三选一,不是两个控件。** 三档确实住在两个存储上,但那是存储
                        的分工——人要表达的是"这个角色我信到什么程度"。 */}
                    <select
                      aria-label={`${item.name}的执行档位`}
                      value={mine?.rung ?? item.execution}
                      disabled={!editable || saving === item.id}
                      onChange={(e) => pickRung(item.id, e.target.value, item.assignee)}
                    >
                      <option value="auto">auto · 全自动</option>
                      <option value="assisted">assisted · 产出先经人确认</option>
                      <option value="manual">manual · 整步交给人</option>
                    </select>
                    {/* **跑在哪儿是一个真正的选择**,不是一行只读文字(PRD 33 · 02)。
                        candidate 给当前运行时:列出来的正是"它现在跑在这儿,但这些工序
                        还没声明兼容这儿"——派发时会被兼容闸挡下的那些。 */}
                    <EmployeeRuntimePicker
                      employee={item.id}
                      candidate={item.runtime}
                      onChanged={() => {
                        api.roster().then(setReport).catch(() => undefined);
                        loadWorkers();
                      }}
                    />
                    {(mine?.employee === item.id || item.execution !== "auto") && (
                      // 这一档的活归谁。**manual 与 assisted 都要能填**:填了不落下去比
                      // 不给填更糟——人以为自己指了个主人,而实际上没有。
                      <div style={{ marginTop: 6 }}>
                        <label htmlFor={`assignee-${item.id}`}>{item.name}的指派人</label>
                        <input
                          id={`assignee-${item.id}`}
                          value={mine?.employee === item.id ? mine.assignee : item.assignee}
                          placeholder="这一档的活归谁"
                          disabled={!editable}
                          onChange={(e) =>
                            setDraft({
                              employee: item.id,
                              rung: mine?.employee === item.id ? mine.rung : item.execution,
                              assignee: e.target.value,
                            })
                          }
                        />
                        {/* 项目配了统一确认人时,这一格改的仍然是员工自己的指派人——
                            不说清楚的话,保存成功而这一档的活还是归别人,没有任何报错。 */}
                        <div className="hint">
                          {item.confirmer && item.confirmer !== item.assignee
                            ? `本项目统一由「${item.confirmer}」确认,这一格只改这个员工自己的指派人`
                            : "没有主人的待办不会被任何人看到,只会静默超时。"}
                        </div>
                        <button
                          className="btn sm"
                          disabled={
                            !editable ||
                            mine?.employee !== item.id ||
                            !mine.assignee.trim() ||
                            saving === item.id
                          }
                          onClick={() =>
                            mine?.employee === item.id &&
                            moveRung(item.id, mine.rung, mine.assignee.trim())
                          }
                        >
                          {becoming ? "转成 manual" : "保存指派人"}
                        </button>
                      </div>
                    )}
                  </td>
                  {/* 只有容器员工有这一格。跑在本地的那些查不到,渲染出来就是空的。 */}
                  {containers && (
                    <td>
                      <WorkerStatusCell status={workers.get(item.id)} />
                      <WorkerRecycleActions
                        employee={item.id}
                        status={workers.get(item.id)?.status ?? "absent"}
                        canEdit={editable}
                        onChanged={loadWorkers}
                      />
                    </td>
                  )}
                  {/* 出场 0 不留白:留白读起来像"没查到",而这里是"真的一次都没上过场"。 */}
                  <td>{item.appearances === 0 ? <span className="m">未上场</span> : item.appearances}</td>
                  <td>{item.tokens.toLocaleString()}</td>
                  <td className="m">{item.summary}</td>
                </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
