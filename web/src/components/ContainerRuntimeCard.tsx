import { useState } from "react";
import { ApiError, api, type ReadinessView, type SettingsView } from "../api/client";
import { Tag } from "../ui/kit";

/**
 * 容器运行时那一段配置,以及"这条链路通不通"。
 *
 * ## 为什么就绪检查值一个按钮
 *
 * 这一段的每个字段填错,症状都要等到**派发任务时**才出现,而那时的错误说的是"平台不可达"
 * ——它不告诉人少填了哪个字段、也不告诉人服务端进程里缺哪个环境变量。就绪检查把这些
 * 挪到点保存之后的下一秒。
 *
 * ## 分项显示,不折叠成红绿灯
 *
 * 平台、存储、Matrix、服务端凭证指向四个不同的运维动作(查平台 / 查桶策略 / 查 Matrix /
 * 补环境变量)。折叠成一个
 * 之后,"哪一项挂了"这个唯一有用的问题就答不出来了。
 *
 * ## 令牌只出现名字
 *
 * 表单里那两格填的是**环境变量名**,值只在服务端进程里。这也是这一段能进界面白名单的
 * 前提——放开它不等于把密钥端上界面。
 */

type RuntimeConfig = SettingsView["runtime"];
type RuntimeEntries = NonNullable<RuntimeConfig["runtimes"]>;
type RuntimeEntry = RuntimeEntries[string];

const AGENTTEAMS = "agentteams";

/** 表单字段:标签 → 配置键。**一份数据**,渲染与回写都读它,不各写一遍。 */
const FIELDS: { key: string; label: string; hint?: string }[] = [
  { key: "endpoint", label: "平台入口", hint: "控制面地址,如 http://<host>:8090" },
  { key: "consumer_token_env", label: "消费令牌环境变量名", hint: "只填变量名,不填令牌本身" },
  { key: "matrix_homeserver", label: "Matrix 入口" },
  { key: "matrix_token_env", label: "Matrix 令牌环境变量名", hint: "只填变量名" },
  { key: "storage_prefix", label: "存储前缀", hint: "mc 别名形式,通常要指到 .../shared" },
];

const ITEM_LABELS: Record<string, string> = {
  platform: "平台",
  storage: "存储",
  matrix: "Matrix 令牌",
  credentials: "服务端凭证",
};

export function ContainerRuntimeCard({
  runtime,
  canEdit,
  onSaved,
}: {
  runtime: RuntimeConfig;
  canEdit: boolean;
  /** 保存成功。**父组件重读配置**,不在这里乐观合并——服务端会给未填字段补默认值,
   * 前端拼一份"以为生效了的"配置,与真正生效的那份迟早在某个默认值上分叉。 */
  onSaved: () => void;
}) {
  const entries: RuntimeEntries = runtime?.runtimes ?? {};
  const configured = AGENTTEAMS in entries;
  // 没配过就默认收起——一个没启用的能力不该让界面变复杂。
  const [open, setOpen] = useState(configured);
  const [draft, setDraft] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      FIELDS.map(({ key }) => [
      key,
      String((entries[AGENTTEAMS] as Record<string, unknown> | undefined)?.[key] ?? ""),
    ]),
    ),
  );
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState("");
  const [checking, setChecking] = useState(false);
  const [report, setReport] = useState<ReadinessView | null>(null);
  const [checkFailed, setCheckFailed] = useState("");

  if (!open) {
    return (
      <section className="card">
        <button type="button" className="btn" onClick={() => setOpen(true)}>
          配置容器运行时(AgentTeams)
        </button>
      </section>
    );
  }

  const save = () => {
    setSaving(true);
    setFailed("");
    // **整份运行时配置一起写**:按段提交的语义是"这一段的全貌",只写 agentteams 的话
    // 同一段里别的运行时会被抹掉。
    // 发出去的是**这个条目我们知道的部分**——未填字段由服务端补默认值,所以类型是
    // Partial:前端替服务端猜默认值的话,那份猜测迟早与真实默认值分叉。
    const patch: Partial<RuntimeEntry> = {
      ...(entries[AGENTTEAMS] ?? {}),
      transport: "matrix-minio",
    };
    for (const { key } of FIELDS) {
      (patch as Record<string, unknown>)[key] = draft[key] ?? "";
    }
    const next = { ...runtime, runtimes: { ...entries, [AGENTTEAMS]: patch } };
    api
      .updateSettings("runtime", next)
      .then(() => onSaved())
      .catch((e: ApiError) => setFailed(e.detail))
      .finally(() => setSaving(false));
  };

  const check = () => {
    setChecking(true);
    setCheckFailed("");
    setReport(null);
    api
      .containerRuntimeReadiness()
      .then(setReport)
      .catch((e: ApiError) => setCheckFailed(e.detail))
      .finally(() => setChecking(false));
  };

  return (
    <section className="card">
      <h3>容器运行时(AgentTeams)</h3>
      <p className="hint">
        令牌只填**环境变量名**——值由服务端进程的环境提供,不进配置、不进版本库。
      </p>
      {FIELDS.map(({ key, label, hint }) => (
        <label key={key}>
          {label}
          <input
            aria-label={label}
            value={draft[key] ?? ""}
            disabled={!canEdit}
            onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
          />
          {hint ? <span className="hint">{hint}</span> : null}
        </label>
      ))}
      <div className="actions">
        <button type="button" className="btn" onClick={save} disabled={!canEdit || saving}>
          {saving ? "保存中…" : "保存"}
        </button>
        {/* **与保存同一个权限。** 端点要 `edit_settings`(它读得到平台与凭证状态),
            这里不 gate 的话,点它的人只会拿到一个 403——一个亮着但永远失败的按钮。 */}
        <button type="button" className="btn" onClick={check} disabled={!canEdit || checking}>
          {checking ? "检查中…" : "就绪检查"}
        </button>
      </div>
      {failed ? <div className="err on">没保存成:{failed}</div> : null}
      {checkFailed ? <div className="err on">检查没跑成:{checkFailed}</div> : null}
      {report ? (
        <ul className="readiness">
          {report.items.map((item) => (
            <li key={item.name}>
              {/* 结论用 `Tag`,与全站同一套色板。裸 span 的话它会跟后面那句话黏成
                  「平台通过平台入口可达且鉴权通过」——读起来像一句话,而不是一个结论。 */}
              <strong>{ITEM_LABELS[item.name] ?? item.name}</strong>{" "}
              <Tag tone={item.ok ? "ok" : "bad"}>{item.ok ? "通过" : "未通过"}</Tag>{" "}
              <span className="m">{item.detail}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
