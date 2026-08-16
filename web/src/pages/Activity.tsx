/**
 * 活动流:三个平面一个入口。
 *
 * 三个记录平面此前各查各的,没有一个地方能回答"上周这个工作空间里,人和系统一共干了什么"。
 *
 * ## 默认视图带预设过滤,不是全量倒序
 *
 * 三平面汇聚听起来很有价值,但**默认全量倒序的话它就是一条谁也读不完的流水**,而一个读不完
 * 的页面等于没有。所以默认是"最近 24 小时的人为动作与配置变更"——把它定位成异常与人为动作
 * 的巡检入口,全量检索留给显式的过滤操作。
 *
 * ## 每条记录说清它在哪个平面
 *
 * 事件面答"什么时候发生了什么",日志面答"员工具体怎么干的",版本面答"资产变成了什么样"。
 * 一条记录不标平面的话,人顺着往下查时不知道该去哪儿看——而这正是汇聚的意义所在。
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type AuditEventPage } from "../api/client";
import { Card, Empty, Note, Tag } from "../ui/kit";

/** 行为主体的类别。**与后端那个枚举一一对应**,不是前端自己发明的一套词。 */
const ACTOR_KINDS = [
  ["", "全部主体"],
  ["human", "人"],
  ["employee", "员工"],
  ["orchestrator", "编排器"],
  ["gate", "门禁"],
  ["integration", "集成入口"],
] as const;

/**
 * 巡检视角关心的那几类:人动过手的,和配置变过的。
 *
 * 迁移那两条**必须在**:人取消一个任务、回答一个闸门、被拒的回答,记的都是迁移——漏掉它们
 * 的话,这个视角恰恰看不见它自己要看的那批介入。
 */
const PATROL_KINDS = [
  "approval",
  "config_changed",
  "escalated",
  "gap_scan",
  "genome_pr",
  "transition",
  "transition_refused",
];

/** 这条记录的内容住在哪个平面。**指针指向哪儿,人就该往哪儿查。** */
export function planeOf(kind: string): { plane: string; where: string } {
  if (kind === "config_changed" || kind === "genome_pr") {
    return { plane: "版本面", where: "内容在提交里" };
  }
  if (kind === "job_started" || kind === "job_finished") {
    return { plane: "日志面", where: "细节在员工的输出流里" };
  }
  return { plane: "事件面", where: "这条本身就是内容" };
}

function hoursAgo(hours: number): string {
  return new Date(Date.now() - hours * 3600_000).toISOString();
}

/**
 * `datetime-local` 给的是**不带时区**的本地时间字符串,而后端按 UTC 补全。原样送过去的话,
 * 一个东八区的审计员会拿到一个整整差八小时的窗口——而界面上它与正确的窗口一模一样。
 */
export function localToIso(value: string): string {
  return value ? new Date(value).toISOString() : "";
}

export function Activity() {
  // **不给"全部工作空间"这个选项。** 后端没有跨空间查询:不带 workspace 的请求在多空间
  // 部署上直接 400,于是那个选项每次都是一次失败——比没有它更糟。
  const [workspace, setWorkspace] = useState("");
  const [spaces, setSpaces] = useState<string[]>([]);
  const [actorKind, setActorKind] = useState("");
  const [actor, setActor] = useState("");
  const [kind, setKind] = useState("");
  const [since, setSince] = useState("");
  const [patrol, setPatrol] = useState(true);
  const [page, setPage] = useState<AuditEventPage | null>(null);
  const [error, setError] = useState("");

  const search = useCallback(() => {
    api
      .auditEvents({
        ...(workspace ? { workspace } : {}),
        ...(actor ? { actor } : {}),
        ...(actorKind ? { actor_kind: actorKind } : {}),
        ...(kind ? { kind } : {}),
        // **类型集合交给服务端筛。** 取回一页再在前端过一遍的话,一个繁忙的 24 小时里
        // 那一页可能全是 Job 事件,于是页面自信地说"没有人为动作",而它们只是排在后面。
        ...(patrol && !kind ? { kinds: PATROL_KINDS.join(",") } : {}),
        // 巡检视角:最近 24 小时。显式给了时间就用人给的。
        ...(since ? { since: localToIso(since) } : patrol ? { since: hoursAgo(24) } : {}),
      })
      .then((found) => {
        setPage(found);
        setError("");
      })
      // 拉不到就说拉不到。空流与"查不出来"在界面上长得一模一样,而后者是要修的。
      .catch((e: ApiError) => setError(e.detail));
  }, [workspace, actor, actorKind, kind, since, patrol]);

  useEffect(() => {
    // 让人手打空间名的话,打错一个字的结果是一页空的检索结果——与"这个空间里真的没有
    // 记录"分不开。拉不到就退回单空间,不把整页判死。
    api
      .workspaces()
      .then((found) => {
        const names = found.items ?? [];
        setSpaces(names);
        if (names.length > 1) setWorkspace((current) => current || names[0]!);
      })
      .catch(() => setSpaces([]));
  }, []);

  useEffect(() => {
    search();
  }, [search]);

  const rows = page?.items ?? [];

  return (
    <Card title="活动流" extra={<span className="m">三个平面一个入口</span>}>
      <div className="filters">
        <button
          className={`btn sm ${patrol ? "pri" : ""}`}
          aria-pressed={patrol}
          onClick={() => setPatrol(!patrol)}
        >
          巡检视角:最近 24 小时的人为动作与配置变更
        </button>
      </div>

      <div className="grid g3">
        {spaces.length > 1 && (
          <select
            aria-label="按工作空间过滤"
            value={workspace}
            onChange={(event) => setWorkspace(event.target.value)}
          >
            {spaces.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        )}
        <select
          aria-label="按行为主体类别过滤"
          value={actorKind}
          onChange={(event) => setActorKind(event.target.value)}
        >
          {ACTOR_KINDS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <input
          aria-label="按行为主体过滤"
          placeholder="具体是谁"
          value={actor}
          onChange={(event) => setActor(event.target.value)}
        />
        <input
          aria-label="按事件类型过滤"
          placeholder="事件类型，如 approval"
          value={kind}
          onChange={(event) => setKind(event.target.value)}
        />
      </div>
      <div style={{ marginTop: 10 }}>
        <label>从</label>
        <input
          aria-label="按时间范围过滤"
          type="datetime-local"
          value={since}
          onChange={(event) => setSince(event.target.value)}
        />
      </div>

      {error && <Note tone="warn">拉不到活动流:{error}</Note>}

      {page && (
        rows.length === 0 ? (
          <Empty>这段时间里没有人为动作或配置变更。</Empty>
        ) : (
          <table style={{ marginTop: 12 }}>
            <thead>
              <tr>
                <th>时间</th>
                <th>主体</th>
                <th>类型</th>
                <th>平面</th>
                <th>去哪儿看</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((item, at) => {
                const { plane, where } = planeOf(item.kind);
                return (
                  <tr key={`${item.ts}-${at}`}>
                    <td className="mono">{new Date(item.ts).toLocaleString()}</td>
                    <td>
                      {item.actor} <Tag tone="mute">{item.actor_kind}</Tag>
                    </td>
                    <td className="mono">{item.kind}</td>
                    <td>
                      <Tag tone={plane === "版本面" ? "pur" : plane === "日志面" ? "warn" : "pri"}>
                        {plane}
                      </Tag>
                    </td>
                    <td className="m">
                      {/* 配置变更跳提交:内容以版本面为准,事件里本来就没有前值后值。 */}
                      {/* **是链接不是标签。** 标签说"内容在提交里"却不带人过去的话,
                          这一页仍然是终点,而它存在的意义正是让人顺着看下去。 */}
                      {item.payload?.rev ? (
                        <a className="mono" href={`#/commits/${String(item.payload.rev)}`}>
                          提交 {String(item.payload.rev).slice(0, 8)}
                        </a>
                      ) : item.task_id && item.task_id !== "system" ? (
                        <a className="mono" href={`#/tasks/${item.task_id}`}>
                          任务 {item.task_id}
                        </a>
                      ) : (
                        where
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )
      )}
    </Card>
  );
}
