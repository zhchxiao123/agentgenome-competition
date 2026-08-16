# Claude Code stream-json 黄金样本

**真实 `claude` 调用的原始未加工输出。** 归一化解析的测试以它为输入,而不是以我们
猜测的格式为输入——PRD 02 把"stream-json 里 token 用量字段的确切形状"列为头号
未验证假设,这两份样本就是去验证它的结果。

| 采集日期 | CLI 版本 | 模型档位 |
|---|---|---|
| 2026-08-08 | Claude Code 2.1.220 | 默认 |

## 两份样本

| 文件 | 是什么 |
|---|---|
| `success.stream.jsonl` + `success.result.json` | 正常完成:建文件、读回、写出结构化结果 |
| `plan-mode-noop.stream.jsonl` | **退出码 0 但什么都没干**——见下 |

## 采集时发现的三件事(都推翻了原定实现)

### 1. 逐行累加 token 会翻倍多算

每行 `assistant` 只携带**一个** content block,同一次 API 响应会拆成多行发出
(thinking 一行、tool_use 一行),这些行带**相同的 `message.id` 与相同的 `usage`**。

    line 5  msg_011Cdq6nB4...  ['thinking']  cache_read=23684
    line 6  msg_011Cdq6nB4...  ['tool_use']  cache_read=23684   ← 同一次响应

所以 `usage` 是**每次 API 请求的快照,不是增量**。正确做法是按 `message.id` 去重后
求和;去重后的和与终态 `result` 行的权威总数逐字相等(实测 181409 == 181409)。

### 1b. 流里的 `output_tokens` 只是下界

`input_tokens` 与两个 cache 字段在流里是**精确**的(去重后与终态权威值逐字相等)。
但 `output_tokens` 不是——它是流式过程中的部分值:

    流里各 message 的 output_tokens: [2, 4, 20, 1]  → 合计 27
    终态权威 output_tokens:                            519

后果是**运行中的预算估算系统性低估**,而 output token 恰恰是贵的那部分。

这不是实现缺陷:结束之前这个数据根本不存在。能做的是诚实对待——运行中的估算是
下界,Job 结束时用终态权威值替换它。所以任务级预算是准的,只有"中途掐断"那一下
是近似的(会比设定的上限多花一些)。

### 2. 权威总量在终态行,还带成本

终态 `result` 行的 `usage` 是权威总数,另有 `total_cost_usd`、`num_turns`、
`stop_reason`、`is_error`、`subtype`、`permission_denials`。做预算时
`total_cost_usd` 比裸 token 更直接。

但它只在**结束时**出现——运行中的预算执行仍然要靠按 message.id 去重的累加。

### 3. 默认权限模式下会静默空转

`plan-mode-noop.stream.jsonl` 是这样一次调用:**退出码 0、没有报错、也没有任何产物**。
Agent 进了 plan mode,写了一份计划然后等人确认,而 headless 场景没有人。

headless 拉起必须显式给权限模式,否则系统会以"成功"的姿态什么都不做。这也正好
说明结果契约为什么必须是硬的——退出码完全不足以判断 Job 是否真的完成了。

## 其余行类型

`system`(subtype `init` / `thinking_tokens`)、`user`(内含 `tool_result`)、
`rate_limit_event`。归一化解析要能跳过不认识的类型而不中断。

## 重新采集

模型版本升级后样本会失真。重采步骤见 `docs/golden-sample-refresh.md`。
