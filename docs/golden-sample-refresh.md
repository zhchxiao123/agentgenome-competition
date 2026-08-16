# 重新采集 Claude Code 黄金样本

`tests/fixtures/golden/claude-code/` 里的样本是真实 `claude` 调用的原始输出。
模型或 CLI 版本升级后它会失真——归一化解析仍然对着旧格式测试,而生产环境已经变了。

## 先跑漂移检测

不必凭感觉判断样本有没有失真——有一条打了标记的测试专门查这件事:

```bash
pytest -m drift tests/drift
```

它跑一次真实 `claude`(约 0.09 美元),断言下面"重采后必看的三件事"仍然成立。
默认不在常规 CI 里跑,因为它烧真 token 且依赖网络。

任一条变红就按下面的步骤重采。**唯一的例外**是
`test_streamed_output_tokens_are_still_only_a_lower_bound` ——它变红是好消息,
说明上游把流里的 `output_tokens` 变准了,那时该做的是去掉降级处理而不是重采。

## 什么时候重采

- `claude` CLI 大版本升级之后;
- 归一化解析出现"生产上对不上、测试却全绿"的现象时;
- 发布前。

## 怎么采

```bash
mkdir -p /tmp/probe/out && cd /tmp/probe
cat > task.md <<'TASK'
你在一个隔离工作区里。请完成这个极小的任务：
1. 用 Bash 创建文件 hello.txt，内容为一行：hello from agentgenome
2. 读回 hello.txt 确认内容
3. 把结构化结果写入 out/result.json，格式为：
   {"task_id": "probe-1", "producer": "probe", "created_at": "<ISO8601>", "passed": true, "changed_files": ["hello.txt"]}
只做这三件事，不要做别的。
TASK

# ① 成功样本
claude -p "$(cat task.md)" --output-format stream-json --verbose --max-turns 12 \
  --permission-mode bypassPermissions --allowedTools Bash Read Write > success.stream.jsonl

# ② 空转样本:去掉 --permission-mode,它会进 plan mode 然后什么都不做
claude -p "$(cat task.md)" --output-format stream-json --verbose --max-turns 12 \
  --allowedTools Bash Read Write > plan-mode-noop.stream.jsonl
```

把两份 jsonl 与 `out/result.json` 覆盖进 `tests/fixtures/golden/claude-code/`,
更新那里 README 的采集日期与 CLI 版本,然后跑测试。

## 重采后必看的三件事

样本存在的意义就是验证这三条假设还成不成立(它们都曾经推翻过原定实现):

1. **同一次 API 响应是否仍拆成多行、带相同 `message.id` 与相同 `usage`。**
   若是,逐行累加仍会翻倍多算,必须按 `message.id` 去重。
2. **按 `message.id` 去重后的和,是否仍等于终态 `result` 行的权威总数。**
   这是"运行中的预算估算准不准"的唯一校验。
3. **不给 `--permission-mode` 时是否仍会静默空转**(退出 0、无产物)。
   若是,headless 拉起必须继续显式指定权限模式。

## 漂移检测运行记录

每次跑完 `pytest -m drift tests/drift` 在这里追加一行。空表意味着"从来没验证过",
和 `CliForge` 冒烟那份文档一样——记录本身就是它有没有被真跑过的唯一证据。

| 日期 | CLI 版本 | 结果 | 备注 |
|---|---|---|---|
| 2026-08-08 | 2.1.220 (Claude Code) | 6/6 通过 | 三条假设仍然成立;`output_tokens` 仍是下界 |
