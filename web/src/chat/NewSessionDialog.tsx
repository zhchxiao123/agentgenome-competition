/**
 * 新建会话对话框。
 *
 * 对话工作台的空状态一直写着「还没有会话 —— 新建一个开始问」,而页面上**没有任何可点的
 * 东西对应这句话**:全仓唯一能建会话的地方是 ⌘J 抽屉,它按当前页面猜员工与模式,不让人选。
 * 设计稿里画着的「＋ 新建会话」按钮从未实现,而 PRD 28 的用户故事里也没有一条覆盖它——
 * **缺口在需求层就已经存在**,不是实现漏做。见 PRD 29。
 *
 * 开不了会话的员工**置灰并写明原因**,而不是让用户选完提交再吃一个 409。能力由后端的
 * 员工列表给出,前端不按运行时名去猜。
 */
import { useEffect, useState } from "react";
import { ApiError, api, type EmployeeSummary } from "../api/client";
import { Card, Note } from "../ui/kit";
import { PERMISSION_HINT, createSession, taskHint, whyNotReady } from "./newSession";

export function NewSessionDialog({
  onCreated,
  onClose,
}: {
  onCreated: (sessionId: string) => void;
  onClose: () => void;
}) {
  const [employees, setEmployees] = useState<EmployeeSummary[]>([]);
  const [employee, setEmployee] = useState("");
  // **缺省只读。** 能力要显式授予,这与员工 `procedures:` 白名单默认给空是同一条道理。
  const [writable, setWritable] = useState(false);
  const [title, setTitle] = useState("");
  const [taskId, setTaskId] = useState("");
  const [tasks, setTasks] = useState<{ id: string; title: string }[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .employees()
      .then((found) => {
        setEmployees(found.items);
        // 默认选第一个**开得了会话的**,而不是第一个——默认选中一个必然失败的员工,
        // 等于让最常见的那条路径从一个错误开始。
        setEmployee((current) => current || (found.items.find((e) => e.can_session)?.id ?? ""));
      })
      .catch((e: ApiError) => setError(e.detail));
    // 关联任务时要挑一个,不让人手敲 id。
    api.tasks().then((found) => setTasks(found.map((t) => ({ id: t.id, title: t.title })))).catch(
      () => setTasks([]),
    );
  }, []);

  const blocked = whyNotReady({ employee, employees });

  const submit = () => {
    if (blocked || busy) return;
    setBusy(true);
    createSession({ employee, writable, title, taskId })
      .then((session) => onCreated(session.id))
      // **错误留在对话框里**,用户改完直接重试,不必从头再来一遍。
      .catch((e: ApiError) => setError(e.detail))
      .finally(() => setBusy(false));
  };

  return (
    // **覆盖层。** 它盖在页面上,不改页面骨架——早先它顶掉对话栏、还连带把证据面板一起
    // 藏了,新建时整页塌掉一半,而用户只是想开个会话。
    <div className="mask" role="dialog" aria-label="新建会话" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <Card title="新建会话">
          {error && <Note tone="warn">{error}</Note>}

      <label htmlFor="ns-employee">员工</label>
      <select
        id="ns-employee"
        value={employee}
        onChange={(event) => setEmployee(event.target.value)}
      >
        <option value="">选一个员工</option>
        {employees.map((item) => (
          // 开不了会话的置灰,并把原因写在选项里——只是灰掉的话,用户只会以为是自己没权限。
          <option key={item.id} value={item.id} disabled={!item.can_session}>
            {/* 显示名优先。`arch-employee` 不是人话——这个字段就是为此加的。 */}
            {item.name || item.id}
            {item.can_session ? "" : ` —— ${item.session_blocked_reason}`}
          </option>
        ))}
      </select>

      {/* **权限只在新建时可选。** 建成之后它是静态标签——见 `newSession.ts` 的模块文档。 */}
      <label htmlFor="ns-writable">
        <input
          id="ns-writable"
          type="checkbox"
          checked={writable}
          onChange={(event) => setWritable(event.target.checked)}
        />{" "}
        让它改代码
      </label>
      <div className="m">{writable ? PERMISSION_HINT.writable : PERMISSION_HINT.readonly}</div>

      {/* **无条件渲染。** 它此前的渲染条件挂在模式上,于是"结对"那一档明明需要任务、
          却没有任何地方能填——界面上摆着一个 100% 失败的选项。任务现在是可选的,
          而"可选"意味着它任何时候都得在。 */}
      <label htmlFor="ns-task">关联任务(可选)</label>
      <select id="ns-task" value={taskId} onChange={(event) => setTaskId(event.target.value)}>
        <option value="">不关联</option>
        {tasks.map((task) => (
          <option key={task.id} value={task.id}>
            {task.id} · {task.title}
          </option>
        ))}
      </select>
      {/* 同一个下拉框在两种权限下作用不同,说清楚它,而不是让用户自己猜。 */}
      {taskId && <div className="m">{taskHint(writable)}</div>}

      <label htmlFor="ns-title">标题</label>
      <input
        id="ns-title"
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        placeholder="以后好在列表里认出它"
      />

      <div style={{ display: "flex", gap: 6, marginTop: 10, alignItems: "center" }}>
        <button className="btn sm pri" disabled={!!blocked || busy} onClick={submit}>
          开始对话
        </button>
        <button className="btn sm" onClick={onClose}>
          取消
        </button>
        {/* 不能提交时说清楚差什么,而不是给一个灰掉却不解释的按钮。 */}
        {blocked && <span className="m">{blocked}</span>}
          </div>
        </Card>
      </div>
    </div>
  );
}
