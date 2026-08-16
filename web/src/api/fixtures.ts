import type { SettingsView } from "./client";

/**
 * 生成类型的**完整**测试值。
 *
 * ADR-0001 的回报是"后端改一个字段名,前端编译期就红"。而 `as never` / `as unknown as T`
 * 把这条正好抹掉:字段少写一个、名字改了一个,测试仍然绿。所以这里补全一次,让别处只写
 * 自己关心的那几个字段——**这一处是唯一会因为 schema 变化而红的地方,那正是它存在的理由。**
 */

export type RuntimeConfigView = SettingsView["runtime"];
export type RuntimeEntryView = NonNullable<RuntimeConfigView["runtimes"]>[string];

/** 一条运行时条目。服务端把字段都回全,所以这里也回全,再让调用方覆盖关心的那几个。 */
export function runtimeEntry(over: Partial<RuntimeEntryView> = {}): RuntimeEntryView {
  return {
    cmd: "",
    consumer_token_env: "",
    endpoint: "",
    matrix_homeserver: "",
    matrix_room: "",
    matrix_token_env: "",
    max_turns: 0,
    mc_cmd: "",
    model_tiers: {},
    storage_prefix: "",
    transport: "",
    worker: "",
    ...over,
  };
}

/** 运行时那一段配置。不给 `runtimes` 就是"这个部署一个运行时都没配"。 */
export function runtimeConfig(
  runtimes: Record<string, RuntimeEntryView> = {},
): RuntimeConfigView {
  return { default: "claude-code", default_is_explicit: false, runtimes };
}
