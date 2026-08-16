/** 用户能看到的任务状态语义。状态机仍由服务端裁决,这里只负责一致地解释它。 */
type TaskAttention = "running" | "waiting" | "attention" | "exception" | "settled";

type DevStatePresentation = {
  readonly label: string;
  readonly step: string;
  readonly stage?: string;
  readonly attention: TaskAttention;
  readonly runLabel?: string;
  readonly summary: string;
  readonly terminal?: boolean;
};

const DEV_STATE: Readonly<Record<string, DevStatePresentation>> = {
  CREATED: { label: "待启动", step: "需求解析", stage: "plan", attention: "waiting", runLabel: "开始执行", summary: "任务已建立,等待开始需求解析。" },
  DEVELOPING: { label: "开发中", step: "开发", stage: "develop", attention: "running", runLabel: "继续推进", summary: "数字员工正在实现本轮需求并持续记录执行结果。" },
  UNIT_TESTING: { label: "单元测试", step: "单元测试", stage: "unit-gate", attention: "running", runLabel: "继续推进", summary: "正在运行单元测试门禁,验证本轮代码变更。" },
  INTEGRATION_TESTING: { label: "集成测试", step: "集成测试", stage: "itest", attention: "running", runLabel: "继续推进", summary: "正在验证跨模块流程和真实集成行为。" },
  READY_TO_COMMIT: { label: "提交前检查", step: "提交前检查", stage: "precheck", attention: "running", runLabel: "继续检查", summary: "正在进行越权复查、密钥扫描和风险评级。" },
  REVIEWING: { label: "待审批", step: "审查", stage: "review", attention: "attention", summary: "变更与证据已准备完成,等待人工审批。" },
  MERGING: { label: "合并中", step: "合并", stage: "merge", attention: "running", runLabel: "继续合并", summary: "审批已通过,正在安全合并变更。" },
  ESCALATED: { label: "需要人工", step: "人工接管", attention: "exception", summary: "自动流程已停手,等待人工接管。", terminal: true },
  COMPLETED: { label: "已完成", step: "已完成", attention: "settled", summary: "任务已完成。", terminal: true },
  CANCELLED: { label: "已取消", step: "已取消", attention: "settled", summary: "任务已取消。", terminal: true },
};

const GENOME_STATE: Readonly<Record<string, { readonly label: string; readonly step: string; readonly summary: string }>> = {
  SCANNING: { label: "扫描中", step: "扫描", summary: "正在发现并整理知识范围。" },
  AWAITING_CONFIRMATION: { label: "等待确认", step: "边界确认", summary: "扫描结果已准备好，等待人工确认知识边界。" },
  DEEP_READ: { label: "深读中", step: "深读", summary: "正在逐模块读取并提炼领域知识。" },
  SUMMARISING: { label: "汇总中", step: "汇总", summary: "正在汇总深读结果并生成知识产物。" },
  SUBMITTED: { label: "已完成", step: "已完成", summary: "知识产物已提交。" },
  FAILED: { label: "执行失败", step: "诊断", summary: "本次知识任务已停止并记录失败原因。" },
  CANCELLED: { label: "已取消", step: "已取消", summary: "本次知识任务已取消。" },
};

export const STAGE_LABEL: Readonly<Record<string, string>> = {
  plan: "需求解析",
  develop: "开发",
  "unit-gate": "单元测试门禁",
  itest: "集成测试",
  "itest-decide": "集成测试判定",
  precheck: "提交前检查",
  review: "审查",
  merge: "合并",
};

export const WORKFLOW_STAGES = [
  ["plan", "需求解析"],
  ["develop", "开发"],
  ["unit-gate", "单元测试"],
  ["itest", "集成测试"],
  ["precheck", "提交前检查"],
  ["review", "审查"],
  ["merge", "合并"],
] as const;

export function taskStateLabel(state: string): string {
  return DEV_STATE[state]?.label ?? GENOME_STATE[state]?.label ?? state;
}

export function taskDisplayLabel(state: string, executionStatus = "idle"): string {
  if (executionStatus === "running" && DEV_STATE[state]) return `${taskStepLabel(state)}中`;
  if (executionStatus === "interrupted") return "执行已中断";
  if (state === "CREATED" && executionStatus === "retry_pending") return "等待重试";
  return taskStateLabel(state);
}

export function taskStepLabel(state: string): string {
  return DEV_STATE[state]?.step ?? GENOME_STATE[state]?.step ?? state;
}

export function taskStage(state: string): string | undefined {
  return DEV_STATE[state]?.stage;
}

export function taskStateSummary(state: string): string {
  return DEV_STATE[state]?.summary ?? GENOME_STATE[state]?.summary ?? "任务状态已更新。";
}

export function taskRunLabel(state: string, planRetries = 0, executionStatus = "idle"): string | undefined {
  if (executionStatus === "interrupted") return "继续执行";
  if (state === "CREATED" && planRetries > 0) return "重试需求解析";
  return DEV_STATE[state]?.runLabel;
}

export function isTerminalDevState(state: string): boolean {
  return DEV_STATE[state]?.terminal === true;
}

export function devNeedsAttention(state: string): boolean {
  return DEV_STATE[state]?.attention === "attention" || DEV_STATE[state]?.attention === "exception";
}

export function devAttention(state: string): TaskAttention | undefined {
  return DEV_STATE[state]?.attention;
}

export function relativeTime(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "时间未知";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}
