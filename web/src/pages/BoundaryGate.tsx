/**
 * 模块边界闸门:在界面上回答"这么分对不对"。
 *
 * 后端把这个节点做成了待确认状态、把草案落了盘,但**界面上没有回答的入口的话,这个设计的
 * 价值只兑现了一半**:人还是得回到终端敲命令,而"全异步"的意义正是让人不必回到终端。
 *
 * ## 四种编辑，一种提交格式
 *
 * 改名、合并、拆分、剔除在数据上统一为**"产出最终列表"**——不为每种动作定义单独的提交格式。
 * 那会让后端多出四条各自要校验的路径,而它们表达的是同一件事:最后要哪几个模块。
 *
 * ## 提交前给差异
 *
 * 人改完一屏之后记不住自己改了什么。不给差异的话,"我确认过了"这句话是没有依据的——而这个
 * 闸门存在的全部理由就是让人真的复核一遍。
 *
 * ## 窄屏必须可用
 *
 * 不是锦上添花。全异步的完整承诺是"人可以关掉终端、隔天从任何入口回答",而这个闸门恰恰最
 * 可能在人不在工位时需要被回答——它前面是一段几十分钟的扫描。所以这里用的是可换行的块状
 * 布局,不是定宽表格。
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type BoundaryModule, type GateDraft } from "../api/client";
import { Card, Empty, Note, Tag } from "../ui/kit";

/** 一行相对草案发生了什么。**给人看的差异,不是提交格式。** */
type Change =
  | { kind: "renamed"; from: string; to: string }
  | { kind: "merged"; id: string; paths: string[] }
  | { kind: "repathed"; id: string; paths: string[] }
  | { kind: "dropped"; id: string }
  | { kind: "added"; id: string };

const samePaths = (a: string[], b: string[]) => a.join("|") === b.join("|");

/**
 * 差异按**目录**认人,不按 id:id 恰恰是人最常改的那一样,按它认的话一次改名会被报成
 * "删了一个、加了一个",而人一眼看不出那其实是同一个模块。
 *
 * 合并单独成一类。把它算成"新增 + 剔除"的话,报告会说他添加了一个草案里本来就有的模块——
 * 而这个面板存在的全部理由,就是让"我确认过了"这句话有依据。
 */
export function diffAgainst(draft: BoundaryModule[], final: BoundaryModule[]): Change[] {
  const owner = new Map<string, BoundaryModule>();
  for (const item of draft) for (const path of item.paths ?? []) owner.set(path, item);

  const changes: Change[] = [];
  const kept = new Set<string>();
  for (const item of final) {
    const paths = item.paths ?? [];
    const origins = [...new Set(paths.map((path) => owner.get(path)).filter(Boolean))];
    if (origins.length === 0) {
      changes.push({ kind: "added", id: item.id });
      continue;
    }
    origins.forEach((origin) => kept.add(origin!.id));
    if (origins.length > 1) {
      changes.push({ kind: "merged", id: item.id, paths });
      continue;
    }
    const origin = origins[0]!;
    if (origin.id !== item.id) changes.push({ kind: "renamed", from: origin.id, to: item.id });
    if (!samePaths(origin.paths ?? [], paths)) {
      changes.push({ kind: "repathed", id: item.id, paths });
    }
  }
  for (const item of draft) {
    if (!kept.has(item.id)) changes.push({ kind: "dropped", id: item.id });
  }
  return changes;
}

export function BoundaryGate({ taskId, onAnswered }: { taskId: string; onAnswered: () => void }) {
  const [draft, setDraft] = useState<GateDraft | null>(null);
  const [rows, setRows] = useState<BoundaryModule[]>([]);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(() => {
    api
      .gateDraft(taskId)
      .then((found) => {
        setDraft(found);
        setRows(found.modules ?? []);
        setError("");
      })
      .catch((e: ApiError) => setError(e.detail));
  }, [taskId]);

  useEffect(load, [load]);

  if (error) return <Empty>拉不到边界草案:{error}</Empty>;
  if (!draft) return <Empty>加载中…</Empty>;

  const patch = (index: number, changes: Partial<BoundaryModule>) =>
    setRows(rows.map((row, at) => (at === index ? { ...row, ...changes } : row)));

  /**
   * 合并:把下一行并进这一行,两份目录接在一起。
   *
   * **接完之后这一行是不合法的**(一个模块对应一个目录),提交会被禁用,由人把它改成一个
   * 共同的父目录。这一步不替他猜:猜错的那次,他合并的第二个目录会静默失去覆盖,而界面上
   * 显示的是"已确认"。
   */
  const mergeDown = (index: number) => {
    const row = rows[index];
    const next = rows[index + 1];
    if (!row || !next) return;
    setRows(
      rows
        .map((item, at) =>
          at === index ? { ...item, paths: [...(row.paths ?? []), ...(next.paths ?? [])] } : item,
        )
        .filter((_, at) => at !== index + 1),
    );
  };

  /**
   * 拆分:一个塞了两个域的目录变成两行。
   *
   * **新那一行的目录留空,由人填。** 复制一份原目录的话,提交出去的是两个声称覆盖同一个
   * 目录的模块——那是一份下游谁都解析不对的划分,而界面上它看起来完全正常。
   */
  const split = (index: number) => {
    const row = rows[index];
    if (!row) return;
    setRows([
      ...rows.slice(0, index + 1),
      { ...row, id: `${row.id}-2`, paths: [], rationale: "" },
      ...rows.slice(index + 1),
    ]);
  };

  const drop = (index: number) => setRows(rows.filter((_, at) => at !== index));

  const changes = diffAgainst(draft.modules ?? [], rows);
  // 一个模块对应一个目录:项目地图上的 path 是单值,而它同时是子模块的挂载点。
  // 在提交前说清楚,好过让人在几十分钟之后从一条失败原因里读到它。
  const wrong = rows.some((row) => (row.paths ?? []).length === 0)
    ? "有模块还没填覆盖的目录——没有目录的模块下游路由不到任何代码。"
    : rows.some((row) => (row.paths ?? []).length > 1)
      ? "一个模块只能覆盖一个目录。合并出来的那一行请改成它们的共同父目录。"
      : "";

  const submit = () => {
    setSubmitting(true);
    api
      .answerGate(taskId, { modules: rows, note })
      .then(() => {
        setError("");
        onAnswered();
      })
      // 提交失败必须说出来。静默吞掉的话,人以为答完了,而任务还停在待确认。
      .catch((e: ApiError) => setError(`提交失败:${e.detail}`))
      .finally(() => setSubmitting(false));
  };

  return (
    <Card title="模块边界待确认" extra={<Tag tone="pur">{draft.state}</Tag>}>
      <Note>{draft.note}</Note>
      {/* 答过之后回来看的是他自己那一版。不说的话,他会以为系统把草案改成了这样。 */}
      {draft.answered && <Note tone="warn">这是你上次提交的那一版,不是原始草案。</Note>}

      <div className="gate">
        {rows.map((row, index) => (
          // **按位置作 key,不按 id 或路径。** 两者都是人正在编辑的字段:拿它们当 key 的话,
          // 每敲一个字符输入框就会被重建一次、焦点丢失,于是"改名"这件事在界面上根本打不完
          // 一个词——而它看起来只是"输入框很卡"。
          <div className="gate-row" key={index}>
            <label>
              模块 id
              <input
                aria-label={`模块 id ${index + 1}`}
                value={row.id}
                onChange={(event) => patch(index, { id: event.target.value })}
              />
            </label>
            <label>
              覆盖的目录(逗号分隔)
              <input
                aria-label={`覆盖的目录 ${index + 1}`}
                value={(row.paths ?? []).join(", ")}
                onChange={(event) =>
                  patch(index, {
                    paths: event.target.value
                      .split(",")
                      .map((item) => item.trim())
                      .filter(Boolean),
                  })
                }
              />
            </label>
            {row.summary && <div className="m">{row.summary}</div>}
            {/* 划分依据是人唯一能复核的东西——只给一个列表的话,他无从判断该不该改它。 */}
            <div className="m">{row.rationale || "(这一行是你加的)"}</div>
            <div className="gate-acts">
              <button className="btn sm" disabled={index + 1 >= rows.length} onClick={() => mergeDown(index)}>
                与下一个合并
              </button>
              <button className="btn sm" onClick={() => split(index)}>
                拆开
              </button>
              <button className="btn sm bad" onClick={() => drop(index)}>
                不建知识
              </button>
            </div>
          </div>
        ))}
      </div>

      <label className="gate-note">
        备注(为什么这么改)
        <textarea value={note} onChange={(event) => setNote(event.target.value)} />
      </label>

      <Card title="与草案的差异">
        {changes.length === 0 ? (
          <Empty>没有改动 —— 提交等于确认这份草案。</Empty>
        ) : (
          <ul>
            {changes.map((change, at) => (
              <li key={at}>
                {change.kind === "renamed" && `改名:${change.from} → ${change.to}`}
                {change.kind === "merged" && `合并:${change.id} 现在覆盖 ${change.paths.join("、")}`}
                {change.kind === "repathed" &&
                  `改目录:${change.id} → ${change.paths.join("、") || "(空)"}`}
                {change.kind === "dropped" && `剔除:${change.id}`}
                {change.kind === "added" && `新增:${change.id}`}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <button className="btn pri" disabled={submitting || rows.length === 0 || !!wrong} onClick={submit}>
        提交并继续
      </button>
      {rows.length === 0 && <Note tone="warn">全部剔除等于没有知识可建,至少留一个模块。</Note>}
      {wrong && <Note tone="warn">{wrong}</Note>}
    </Card>
  );
}
