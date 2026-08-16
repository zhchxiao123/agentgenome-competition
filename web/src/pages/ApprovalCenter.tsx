/**
 * 审批中心:一屏做决策。
 *
 * diff、验收标准、质量结论、风险与命中规则、决策区全在一屏。分成几页的话审批人会跳过其中
 * 几个,然后凭 diff 的长度做判断——那等于没有审批。
 *
 * **决策区吸顶。** 真实 diff 比示例长得多,滚下去之后按钮要还在。
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type ArtifactList, type LineComment, type ReportResponse, type TaskSummary } from "../api/client";
import { subscribe } from "../api/live";
import { DiffView } from "../ui/DiffView";
import { LogViewer } from "../ui/LogViewer";
import { Card, Empty, Note, Tag } from "../ui/kit";

/** 结构化的驳回原因。**勾选项比纯文本框更容易回注**——它们能当标签用。 */
const REASONS = [
  "功能需求不完整",
  "技术方案存在风险",
  "测试覆盖不足",
  "影响范围评估不清晰",
  "其他",
] as const;

export function ApprovalCenter({ actor }: { actor: string }) {
  const [queue, setQueue] = useState<TaskSummary[]>([]);
  const [picked, setPicked] = useState<string | null>(null);
  const [lineComments, setLineComments] = useState<LineComment[]>([]);
  // 之前这里没有 `.catch(...)`——拉取失败是一个悬空的 rejected promise,队列停留在初始
  // 空数组,界面上跟"真的没有待审批任务"完全一样。
  const [error, setError] = useState("");

  const reload = useCallback(() => {
    api
      .tasks()
      .then((all) => {
        const waiting = all.filter((task) => task.state === "REVIEWING");
        setQueue(waiting);
        setPicked((current) =>
          current && waiting.some((task) => task.id === current)
            ? current
            : waiting[0]?.id ?? null,
        );
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
  }, []);

  useEffect(() => {
    reload();
    return subscribe(reload);
  }, [reload]);

  const pick = (id: string) => {
    setPicked(id);
    setLineComments([]);
  };

  return (
    <>
      <h1>审批中心</h1>
      <div className="sub">集中处理高风险任务的审批 —— 一屏做完决策</div>

      {error ? (
        <Empty>拉不到待审批队列:{error}</Empty>
      ) : queue.length === 0 ? (
        <Empty>没有等着审批的任务。</Empty>
      ) : (
        <>
          <div className="grid" style={{ gridTemplateColumns: "270px 1fr 268px", alignItems: "start" }}>
            <Card>
              {queue.map((task) => (
                <div
                  className="tk"
                  key={task.id}
                  onClick={() => pick(task.id)}
                  style={picked === task.id ? { borderColor: "var(--pri)", background: "var(--pri-bg)" } : undefined}
                >
                  <Tag tone="bad">{task.risk_level ?? "high"}</Tag>
                  <b>{task.title}</b>
                  <div className="m mono">{task.id}</div>
                </div>
              ))}
              <Note>超时<b>不自动放行</b>。队列积压会出现在观测中心的指标里。</Note>
            </Card>

            {picked ? <Evidence id={picked} /> : <Empty>选一个</Empty>}
            {picked && (
              <Decide
                id={picked}
                actor={actor}
                lineComments={lineComments}
                onDone={() => { setPicked(null); setLineComments([]); reload(); }}
              />
            )}
          </div>

          {picked && (
            <DiffAnnotator id={picked} lineComments={lineComments} onAdd={(c) => setLineComments((v) => [...v, c])} onRemove={(index) => setLineComments((v) => v.filter((_, i) => i !== index))} />
          )}
        </>
      )}
    </>
  );
}

function Evidence({ id }: { id: string }) {
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setError("");
    setReport(null);
    api
      .report(id)
      .then(setReport)
      .catch((e: ApiError) => setError(e.detail));
  }, [id]);

  return (
    <Card title="材料">
      {/* 报告是确定性渲染的,含需求、计划、经过、质量、变更、证据、成本。
          评审者不该为了判断能不能合去四处翻产物目录。 */}
      <LogViewer text={error ? `拉不到材料:${error}` : (report?.markdown ?? "加载中…")} maxHeight={420} />
    </Card>
  );
}

function Decide({
  id,
  actor,
  lineComments,
  onDone,
}: {
  id: string;
  actor: string;
  lineComments: LineComment[];
  onDone: () => void;
}) {
  const [reasons, setReasons] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [error, setError] = useState("");
  const [preview, setPreview] = useState("");

  const composed = () => (reasons.length || comment.trim() ? `${reasons.join("、")}${reasons.length ? ":" : ""}${comment.trim()}` : "");

  const showPreview = () => {
    api
      .previewRejection(id, composed(), lineComments)
      .then((body) => setPreview(body.text))
      .catch((e: ApiError) => setError(e.detail));
  };

  const reject = () => {
    // 后端已经强制(ApprovalRefused)。前端提前拦是为了让人在写之前就知道为什么要写。
    if (!composed().trim() && lineComments.length === 0) {
      setError("驳回必须至少留一条意见(整体或行级)。没有意见的驳回对下一轮没有任何价值。");
      return;
    }
    api
      .decide(id, { actor, approved: false, comment: composed(), line_comments: lineComments })
      .then(onDone)
      .catch((e: ApiError) => setError(e.detail));
  };

  return (
    <div style={{ position: "sticky", top: 14 }}>
      <Card title="审批操作">
        <button
          className="btn ok blk"
          onClick={() =>
            api
              .decide(id, { actor, approved: true, comment: "" })
              .then(onDone)
              .catch((e: ApiError) => setError(e.detail))
          }
        >
          ✓ 通过审批
        </button>

        <label>驳回原因</label>
        <div className="cbs">
          {REASONS.map((reason) => (
            <label key={reason}>
              <input
                type="checkbox"
                checked={reasons.includes(reason)}
                onChange={(e) =>
                  setReasons((current) =>
                    e.target.checked ? [...current, reason] : current.filter((x) => x !== reason),
                  )
                }
              />
              {reason}
            </label>
          ))}
        </div>

        <label>整体说明</label>
        <textarea
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="行级问题请挂在下方 diff 的具体行上 —— 精确到行的批注,对下一轮的价值高一个量级"
        />
        {lineComments.length > 0 && (
          <Note>已挂 {lineComments.length} 条行级批注,会与整体说明一起回注。</Note>
        )}
        {error && <div className="err on">{error}</div>}
        <button className="btn sm" style={{ marginTop: 9, width: "100%" }} onClick={showPreview}>
          预览将要回注的完整意见
        </button>
        {preview && (
          <div style={{ marginTop: 8 }}>
            <LogViewer text={preview} maxHeight={160} />
          </div>
        )}
        <button className="btn bad blk" style={{ marginTop: 9 }} onClick={reject}>✕ 驳回</button>
        <Note>这是人的判断唯一能直接影响下一轮的通道 —— <b>提交前预览显示的就是实际发送的</b>。</Note>
      </Card>
    </div>
  );
}

/** diff 批注:挑一个产物按行查看,点行号挂一条批注。 */
function DiffAnnotator({
  id,
  lineComments,
  onAdd,
  onRemove,
}: {
  id: string;
  lineComments: LineComment[];
  onAdd: (comment: LineComment) => void;
  onRemove: (index: number) => void;
}) {
  const [artifacts, setArtifacts] = useState<ArtifactList | null>(null);
  const [path, setPath] = useState<string | null>(null);
  const [lines, setLines] = useState<string[]>([]);
  const [drafting, setDrafting] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    setPath(null);
    setLines([]);
    setError("");
    api
      .artifacts(id)
      .then(setArtifacts)
      .catch((e: ApiError) => setError(e.detail));
  }, [id]);

  useEffect(() => {
    if (!path) return;
    api.artifact(id, path).then((text) => setLines(text.split("\n")));
  }, [id, path]);

  const addComment = (line: number) => {
    if (!path || !draft.trim()) return;
    onAdd({ file: path, line, side: "new", content: draft.trim() });
    setDrafting(null);
    setDraft("");
  };

  return (
    <Card title="Diff 批注" extra={<span className="m">在具体一行上留意见,比总意见精确一个量级</span>}>
      {error ? (
        <Empty>拉不到产物列表:{error}</Empty>
      ) : !artifacts || artifacts.items.length === 0 ? (
        <Empty>这个任务还没有可查看的产物</Empty>
      ) : (
        <>
          <div className="chips" style={{ marginBottom: 10, flexWrap: "wrap" }}>
            {artifacts.items.map((item) => (
              <span key={item.path} className={path === item.path ? "on" : ""} onClick={() => setPath(item.path)}>
                {item.path}
              </span>
            ))}
          </div>

          {path && (
            <DiffView
              lines={lines.map((text) => ({ text }))}
              onLineClick={(lineNo) => setDrafting(drafting === lineNo ? null : lineNo)}
              renderUnderLine={(lineNo) => {
                const attached = lineComments.filter((c) => c.file === path && c.line === lineNo);
                return (
                  <>
                    {attached.map((c, i) => (
                      <div key={i} className="note" style={{ margin: "0 11px 6px" }}>
                        {c.content}{" "}
                        <button
                          className="btn sm"
                          style={{ marginLeft: 8 }}
                          onClick={() => onRemove(lineComments.indexOf(c))}
                        >
                          删除
                        </button>
                      </div>
                    ))}
                    {drafting === lineNo && (
                      <div style={{ margin: "0 11px 8px", display: "flex", gap: 6 }}>
                        <input
                          autoFocus
                          value={draft}
                          onChange={(e) => setDraft(e.target.value)}
                          placeholder="这一行的问题是……"
                          onKeyDown={(e) => e.key === "Enter" && addComment(lineNo)}
                        />
                        <button className="btn sm pri" onClick={() => addComment(lineNo)}>挂上</button>
                      </div>
                    )}
                  </>
                );
              }}
            />
          )}
        </>
      )}
    </Card>
  );
}
