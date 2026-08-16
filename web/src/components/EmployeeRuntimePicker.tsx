import { useEffect, useState } from "react";
import { ApiError, api, type RuntimeChoiceView } from "../api/client";

/**
 * 一个员工跑在哪儿——以及换过去之前还差什么。
 *
 * ## 选项由服务端给
 *
 * 前端枚举一份的话,配置里没配的运行时会出现在下拉框里;选中之后装配失败,而那次失败
 * 发生在**下一次派发**,不在点击那一刻。人于是要在两个时间点之间自己连线。
 *
 * ## 兼容缺口列出来,但不自动补
 *
 * 兼容闸的语义是"只在一个运行时上验证过的工序不该悄悄换台跑"。界面替人自动补声明,
 * 等于把这道闸门变成一个没人看得见的摆设。这里只负责**把差距摆在他面前**,并让补声明
 * 只要一次点击——判断仍归人。
 */
export function EmployeeRuntimePicker({
  employee,
  candidate = "",
  onChanged,
}: {
  employee: string;
  /** 想切到哪个运行时。给了才查兼容缺口——不给就只是展示现状。 */
  candidate?: string;
  /** 改成了。**父组件重读花名册**——运行时是它那一行的数据,不该有第二份。 */
  onChanged?: () => void;
}) {
  const [choice, setChoice] = useState<RuntimeChoiceView | null>(null);
  const [failed, setFailed] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => {
    api
      .employeeRuntime(employee, candidate)
      .then(setChoice)
      .catch((e: ApiError) => setFailed(e.detail));
  };

  useEffect(load, [employee, candidate]);

  if (!choice) {
    return <span className="hint">读取运行时…</span>;
  }

  const pick = (runtime: string) => {
    setBusy(true);
    setFailed("");
    api
      .setEmployeeRuntime(employee, runtime)
      // 成功之后重读,而不是把选中值当成已生效——服务端可能因为别的原因落到别的值上。
      .then(() => {
        load();
        onChanged?.();
      })
      .catch((e: ApiError) => setFailed(e.detail))
      .finally(() => setBusy(false));
  };

  const gap = choice.compat_gap ?? [];
  // 接不了这个员工的运行时。**与兼容缺口分开**:缺口补一次声明就没了,这一类补不掉。
  const blocked = new Map((choice.blocked ?? []).map((one) => [one.runtime, one.reason]));

  const declare = () => {
    setBusy(true);
    setFailed("");
    api
      .declareCompat(employee, candidate, gap)
      .then(load)
      .catch((e: ApiError) => setFailed(e.detail))
      .finally(() => setBusy(false));
  };

  return (
    <div className="runtime-picker">
      <label>
        运行时
        <select
          aria-label="运行时"
          value={choice.current}
          disabled={!choice.can_edit || busy}
          onChange={(event) => pick(event.target.value)}
        >
          {choice.options.map((option) => (
            // 接不了的**选不动**,而不是选完之后失败:后者会让人先把一个运行时写进
            // 版本化资产,再对着一句听起来毫不相干的报错发愣。
            <option key={option} value={option} disabled={blocked.has(option)}>
              {option}
              {blocked.has(option) ? "(接不了这个员工)" : ""}
            </option>
          ))}
        </select>
      </label>
      {blocked.size > 0 ? (
        <ul className="runtime-blocked">
          {[...blocked].map(([runtime, reason]) => (
            <li key={runtime} className="warn">
              {reason}
            </li>
          ))}
        </ul>
      ) : null}
      {failed ? <div className="err on">{failed}</div> : null}
      {gap.length > 0 ? (
        <div className="compat-gap">
          <p>
            这些工序还没声明兼容 <strong>{candidate}</strong>,派发时会被兼容闸挡下:
          </p>
          <ul>
            {gap.map((procedure) => (
              <li key={procedure}>{procedure}</li>
            ))}
          </ul>
          <button type="button" className="btn sm" onClick={declare} disabled={!choice.can_edit || busy}>
            为这些工序声明兼容
          </button>
        </div>
      ) : null}
    </div>
  );
}
