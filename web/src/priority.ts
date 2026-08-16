export const PRIORITIES = [
  { level: "P0", value: 8, description: "紧急" },
  { level: "P1", value: 6, description: "高" },
  { level: "P2", value: 5, description: "中" },
  { level: "P3", value: 2, description: "低" },
] as const;

export function priorityLabel(value: number): string {
  return PRIORITIES.find((item) => item.value === value)?.level ?? `未知优先级（${value}）`;
}
