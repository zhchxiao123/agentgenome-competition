/**
 * 新建会话:**抽屉与工作台共用的唯一一段**。
 *
 * 两处各写一份的话三个月后必然分叉——理由与 `ui/` 那三个共享组件被抽出来时完全相同。
 * 抽屉只是把员工与选项预填好,走的仍然是这里。
 *
 * ## 两个自由度,不是三选一
 *
 * 一次会话只有两件事可选:**让不让它改代码**、**关不关联某个任务**。工作目录由这两者
 * 在后端推出来,不是第三个选项。
 *
 * 早先这里是三个 chip(咨询/质询/结对),它把三件互不相干的事焊在了一起:工作目录、
 * 写权限、上下文预载。焊在一起的直接后果就是「结对」那个 chip——它需要一个任务,而任务
 * 下拉框只对「质询」渲染,于是选中它的人没有任何地方能填,提交必然失败。见 PRD 45。
 *
 * ## 权限只在这里选
 *
 * PRD 28 那条「模式是静态标签不是 Tab」约束的是**已存在**的会话,它防的是中途提权。
 * 新建时选权限不触碰那条约束,反而是它的前提——权限必须在某处被选定,而唯一安全的
 * 地方就是创建时。已建会话的展示形态不变,仍是静态标签。
 */
import { api, type EmployeeSummary, type SessionSummary } from "../api/client";

/** 新建会话的两个自由度。 */
export type SessionDraft = {
  employee: string;
  /** 让它改代码吗。缺省否——**能力要显式授予**。 */
  writable: boolean;
  /** 关联哪个任务。空串表示不关联。 */
  taskId: string;
  title: string;
};

/** 权限 → 人话。**选之前就要看得懂**,不该逼用户去读文档才知道该选哪个。 */
export const PERMISSION_HINT = {
  readonly: "只读。看得见项目代码,但不会动它。",
  writable: "可以改代码。改动落在这个会话自己的分支上,要转成任务才能进主线。",
} as const;

/**
 * 关联任务这件事对这次会话意味着什么。
 *
 * 同一个下拉框,两种权限下作用不同——**说清楚它,而不是让用户自己猜**。
 */
export function taskHint(writable: boolean): string {
  return writable
    ? "在这个任务的隔离工作区里改;它必须是 `--interactive` 建的、且已经进到开发态。"
    : "预载这个任务的产物与日志,聊完可以把结论回注成审批意见。";
}

/**
 * 提交之前先在本地拦一道。
 *
 * 返回一句给人看的话;没问题返回空串。
 *
 * **只剩员工这一项。** 早先还有一条"必须指定任务"——那条的前提是"质询"作为一种模式
 * 存在。现在任务是可选的,没有哪种组合强制要它,那条拦截也就没有了对应的现实。
 *
 * 后端仍会因为"任务不是交互式"或"工作区还没建好"回 400/409,那些是它才知道的事;
 * 对话框把错误留在原地让用户改完重试,不必在这里预演一遍。
 */
export function whyNotReady(draft: {
  employee: string;
  employees: EmployeeSummary[];
}): string {
  if (!draft.employee) return "先选一个员工";
  const picked = draft.employees.find((item) => item.id === draft.employee);
  // 后端算过一遍了,这里只是把它的结论摆到用户眼前。
  if (picked && !picked.can_session) {
    return picked.session_blocked_reason || "这个员工开不了会话";
  }
  return "";
}

/** 真的去建。参数形状由生成类型保证,不手写。 */
export function createSession(draft: SessionDraft): Promise<SessionSummary> {
  return api.createSession({
    employee: draft.employee,
    writable: draft.writable,
    title: draft.title,
    task_id: draft.taskId || null,
  });
}
