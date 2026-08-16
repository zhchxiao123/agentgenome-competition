---
status: accepted
---

# 验证产物,不验证自述:知识类工序的裁决原则

知识类工序(knowledge-init / update / reinit / distill)的契约原本是一个巨型
`result.json`:整篇 Markdown 转义后塞进 JSON 字符串(`doc_markdown` /
`card_markdown` 这类字段),编排器再解包写成真实文件。这是结构化输出的最坏形态,
失败物理学明确:转义是 LLM 最易错的动作;长输出会截断;50 张卡里 1 个转义错误让
整份 JSON 解析失败、全部重新生成;`modules[3].features[7]` 式的 JSON path 报错
回注给下一次尝试基本无法定位。

而最根本的矛盾是:坐在文件系统上的 coding agent,被工具层强制保证的最可靠动作就是
**写文件**——却被要求把文件走私进 JSON 信封,违反系统自己的第一原则「文件即协议」。

## 决定

1. **产出即文件。** 员工在产物目录 `staging/` 下直接写真实的树文件,目录形状与
   最终落点(`genome/knowledge/`)逐字节同构——不存在中间表示,树的校验规则
   (loader / FeatureEntry / 预算)直接复用,不为 staging 再发明一套 schema。
2. **裁决即校验。** Job 成败判据 = 对 `staging/` 跑确定性校验(`genome/staging.py`,
   经 `JobSpec.output_check` 挂进契约检查),全绿即成功。**自述是最弱证据,产物是
   最强证据**:小票声称一切正常而校验失败,Job 失败;没有 staging 树就是没有产物。
3. **`result.json` 降级为小票。** 扁平小 JSON(notes / questions / low_confidence),
   只补充校对清单,任何字段都不参与成败判定。小而平的 JSON 与大嵌套 JSON 是两种
   可靠性完全不同的东西。
4. **报错逐文件,重试增量修复。** 校验器每条错误定位到文件、按文件分组;契约重试时
   `output_dir`(含 staging/)保留在原地,回注的拒绝原因是逐文件清单——第二次尝试
   只修坏的文件。原子失败变局部失败,重试成本从"全量重生成"降为"修几个文件"。
5. **事务性照旧。** 校验通过后由编排器把已验证的文件原子应用进 `genome/knowledge/`
   (快照 + 失败逐字节回滚),"半更新状态的树不合法"原样保留。

## 适用范围

一切"产出是树片段"的工序:knowledge-init / update / reinit / 补卡,以及 distill 的
经验卡片(`staging/lessons/*.md`),统一走 `staging → 校验 → 原子应用`,由工序声明
`outputs.staging` 接入。**代码类工序零改动**——code-develop / unit-gate /
secure-commit 的裁决(门禁与确定性管线)本来就是"验证产物"模式,这次改造是让知识类
工序向它们看齐。L2 规则提案仍走 PR 路径:它的评审者是人,不是校验器。

## 刻意不做

- **不做"全部 md"。** map.yaml、front matter 仍是结构化——机器要用 if/for 消费它们。
  消灭的不是结构,是结构的爆炸半径:从"一个 payload 全对或全错"变成"逐文件成败、
  逐文件报错、局部修复"。
- **不动契约重试外环。** "格式失败重试一次"原语保留,只是"格式失败"的定义从
  "JSON 不合 schema"扩展为"staging 校验不过 / 小票缺失"。

守卫:`tests/unit/test_no_json_envelope_fields.py` 保证"文件进信封"的字段在代码与
契约里零残留。
