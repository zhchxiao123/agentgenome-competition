/**
 * 需求提交:产品经理能自己填。
 *
 * 这是这个页面存在的全部理由。填不动的话,需求还是得工程师代提,那这套系统的入口就仍然
 * 在终端里。
 *
 * 右栏「提交之后会发生什么」不是装饰:产品经理要敢自己提,前提是他知道提完不会立刻改到线上。
 * **它随选中的策略变**——写死一份单路流程的话,选了精化环的人看到的是另一条流水线的说明,
 * 而那是这一页唯一会主动说假话的地方。
 */
import { useEffect, useRef, useState } from "react";
import { ApiError, api, type TaskDetail, type TopologyOption } from "../api/client";
import { PRIORITIES } from "../priority";
import { Card, Note, Tag } from "../ui/kit";

export function Submit({
  onSubmitted,
  disabledReason,
}: {
  onSubmitted: (task: TaskDetail) => void;
  /** 非空表示这个项目现在提不了任务(还在初始化)。禁用入口并把原因摆出来——
      比点了提交才被 409 拒绝早一步。 */
  disabledReason?: string;
}) {
  const [requirement, setRequirement] = useState("");
  const [title, setTitle] = useState("");
  const [itest, setItest] = useState<"auto" | "always" | "never">("auto");
  const [priority, setPriority] = useState(5);
  const [error, setError] = useState("");
  const [ticketUrl, setTicketUrl] = useState("");
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  // 拓扑名单来自后端。**空数组是合法状态**:拉不到就退回一个没有这一栏的表单,而不是
  // 让人提不了需求——策略是可选项,需求原文才是这一页的理由。
  const [options, setOptions] = useState<TopologyOption[]>([]);
  const [projectDefault, setProjectDefault] = useState("");
  const [topology, setTopology] = useState("");

  useEffect(() => {
    api
      .topologies()
      .then((catalog) => {
        setOptions(catalog.options);
        setProjectDefault(catalog.default);
      })
      .catch(() => setOptions([]));
  }, []);

  const chosen = options.find((item) => item.id === topology);
  // 没选就按项目缺省那条流程说话——"跟随项目缺省"如果配一份泛泛的说明,等于什么都没说。
  const shown = chosen ?? options.find((item) => item.id === projectDefault);
  const blocked = options.filter((item) => !item.available);

  const submit = () => {
    if (submittingRef.current || importing) return;
    if (!requirement.trim()) {
      setError("需求原文不能为空 —— 它是任务的全部起点,会原样进架构员工的上下文。");
      return;
    }
    submittingRef.current = true;
    setSubmitting(true);
    setError("");
    api
      // 表单提的都是自主任务;结对走对话工作台的「开结对会话」入口。
      .submit({ requirement, title, itest, priority, mode: "autonomous", topology })
      // 把整个任务交回去:去向(需求详情还是任务详情)由 App 按 requirement_id 决定。
      .then((task) => onSubmitted(task))
      .catch((e: ApiError) => {
        submittingRef.current = false;
        setSubmitting(false);
        setError(e.detail);
      });
  };

  const importTicket = () => {
    if (!ticketUrl.trim() || submittingRef.current || importing) return;
    setImporting(true);
    setImportError("");
    api
      .importTicket(ticketUrl.trim())
      .then((found) => {
        setTitle(found.title);
        setRequirement(found.body);
      })
      .catch((e: ApiError) => setImportError(e.detail))
      .finally(() => setImporting(false));
  };

  return (
    <>
      <h1>需求提交</h1>
      <div className="sub">通过模板化表单快速发起研发任务</div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 300px", alignItems: "start" }}>
        <Card>
          <label>从工单链接导入 <span className="hint">· 目前支持 GitHub issue</span></label>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              value={ticketUrl}
              disabled={submitting || importing}
              onChange={(e) => setTicketUrl(e.target.value)}
              placeholder="https://github.com/<owner>/<repo>/issues/<number>"
            />
            <button className="btn" onClick={importTicket} disabled={importing || submitting}>
              {importing ? "导入中…" : "导入"}
            </button>
          </div>
          {importError && <div className="err on">导入失败:{importError}</div>}

          <label>需求原文 <span className="req">*</span></label>
          <textarea
            value={requirement}
            disabled={submitting || importing}
            onChange={(e) => setRequirement(e.target.value)}
            placeholder="写清楚「谁、在什么情况下、要得到什么」。这段会原样进架构员工的上下文。"
          />
          <div className="cnt">{requirement.length} / 2000</div>

          <label>标题 <span className="hint">· 留空取需求首行</span></label>
          <input value={title} onChange={(e) => setTitle(e.target.value)} disabled={submitting || importing} />

          {options.length > 0 && (
            <>
              <label htmlFor="topology">
                执行拓扑 <span className="hint">· 这一步内部怎么协作</span>
              </label>
              <select
                id="topology"
                value={topology}
                disabled={submitting || importing}
                onChange={(e) => setTopology(e.target.value)}
              >
                {/* 空串是"跟随项目缺省",不是 single。**别替人展开**:展开之后"没表态"
                    与"明确选了 single"在记录上就分不开了。 */}
                <option value="">跟随项目缺省{projectDefault && ` · ${projectDefault}`}</option>
                {options.map((item) => (
                  <option key={item.id} value={item.id} disabled={!item.available}>
                    {item.name} · {item.id}
                    {item.experimental ? "(实验中)" : ""}
                    {/* **倍数写在选项上,不必先选中才知道。** 要人在展开下拉的那一刻就
                        看见"这个贵三倍",而不是选完之后才被告知。 */}
                    {item.cost_multiplier > 1 ? ` · 约 ${item.cost_multiplier} ×` : ""}
                  </option>
                ))}
              </select>
              {shown && <div className="m">{shown.summary}</div>}
              {shown && shown.cost_multiplier > 1 && (
                <div className="err on">
                  成本约 {shown.cost_multiplier} × 单路
                  {/* 绝对值只在算得出来的时候说。**编不出来的数字就不显示**——一个假的
                      绝对值比不显示更糟,因为人会拿它做决定。 */}
                  {shown.cost_estimate_tokens != null &&
                    `,按本项目历史估 ${shown.cost_estimate_tokens.toLocaleString()} tokens`}
                </div>
              )}
              {shown?.experimental && (
                // 机制在、结论还没有(PRD 39 的开工闸)。**这个标记不该被读成推荐。**
                <div className="hint">实验中:成本 {shown.cost_multiplier} 倍,收益尚无数据</div>
              )}
              {blocked.map((item) => (
                // 灰掉一个选项而不说原因,读起来像"这个能力不存在"。
                <div key={item.id} className="hint">
                  {item.name}现在选不了:{item.unavailable_reason}
                </div>
              ))}
            </>
          )}

          <div className="grid g2" style={{ gap: 14 }}>
            <div>
              <label>集成测试</label>
              <select value={itest} disabled={submitting || importing} onChange={(e) => setItest(e.target.value as typeof itest)}>
                <option value="auto">auto · 由影响规则与 AI 兜底判定</option>
                <option value="always">always · 无论如何都跑</option>
                <option value="never">never · 跳过</option>
              </select>
            </div>
            <div>
              <label>优先级</label>
              <select value={priority} disabled={submitting || importing} onChange={(e) => setPriority(Number(e.target.value))}>
                {PRIORITIES.map((item) => (
                  <option key={item.level} value={item.value}>{item.level} {item.description}</option>
                ))}
              </select>
            </div>
          </div>

          {error && <div className="err on">{error}</div>}
          {disabledReason && <Note tone="warn">{disabledReason}</Note>}
          <button
            className="btn pri"
            style={{ marginTop: 18 }}
            onClick={submit}
            disabled={Boolean(disabledReason) || submitting || importing}
          >
            {submitting ? "提交中…" : "提交需求"}
          </button>
        </Card>

        <Card
          title="提交之后会发生什么"
          extra={shown ? <Tag>{shown.name}</Tag> : undefined}
        >
          {/* 拉不到名单时说清楚,不留一片空白:这一栏是产品经理敢自己提需求的前提,
              空着会让人以为"提交之后什么都不会发生"。 */}
          {!shown && <Note tone="warn">拉不到执行拓扑的说明,流程照常跑,只是这里显示不出来。</Note>}
          <table>
            <tbody>
              {(shown?.steps ?? []).map((step, index) => (
                <tr key={step}>
                  <td className="mono" style={{ width: 22 }}>{index + 1}</td>
                  <td>{step}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Note>随时可以取消。终态会生成 <b>report.md</b>,能直接贴进 PR。</Note>
        </Card>
      </div>
    </>
  );
}
