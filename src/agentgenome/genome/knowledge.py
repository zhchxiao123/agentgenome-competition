"""knowledge-init:让数字员工"带着认知上岗"。

`agctl init` 确定性地生成了项目地图骨架(只有 id 与 path)。这里由架构员工通读代码,
把认知类字段补齐,并为每个模块写认知卡片。

## 产出即文件(PRD 34)

员工的产出不再是一个巨型 `result.json`(整篇 Markdown 转义后塞进 JSON 字符串),而是
**产物目录 `staging/` 下真实的树文件**——目录形状与 `genome/knowledge/` 逐字节同构。
坐在文件系统上的 coding agent,被工具层强制保证的最可靠动作就是写文件;让它把文件
走私进 JSON 信封,违反的是这套系统自己的第一原则「文件即协议」。

Job 成败由 `genome.staging` 的确定性校验裁决(验证产物,不验证自述)。`result.json`
降级为小票:notes / questions / low_confidence 三个指针字段,只补充校对清单,
不参与成败判定。

## 三条边界

**id 与 path 不可变。** 它们是 init 从 `.gitmodules` 算出来的事实,不是员工的判断。
staging 里出现未知模块一律拒绝——凭空发明模块会让下游的影响判定失去依据。

**只写产物目录。** 业务代码由开发员工改,架构员工不碰;`genome/knowledge/` 也不由它
直接写——staging 校验全绿之后由编排器原子应用。这不靠员工自觉:越权检查在池里无条件
执行,staging 之外的写入会被回滚。

**人工修改优先。** 卡片里出现 `<!-- human-edited -->` 标记时,重跑不覆盖它;功能点
entry 上的 `human_edited: true` 锁整条 entry(尤其是 `scope`),重跑对同一个 id 的
报告整条忽略。不这样的话"我改过的东西下次扫描就没了",没人会愿意去改。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agentgenome.genome.models import ProjectMap
from agentgenome.genome.staging import RECEIPT_SCHEMA, STAGING_DIR

PROCEDURE_ID = "knowledge-init"
#: 2.0.0:产出通道从 JSON 信封换成 staging 树(PRD 34)。契约变了,版本跟着变——
#: 事件流里的 `procedure@version` 要能说清"当时是哪一版契约干的活"。
#: 2.1.0:卡片骨架换成内容纪律(PRD 41):分节 + 减法筛 + 证据锚点。通道没变,
#: 但"什么算一张合格的卡"变了——版本要能区分新旧纪律下写出的卡。
#: 2.2.0:提示词补齐 interfaces.yaml 的严格字段契约。此前只给文件名,
#: 员工会猜出 name/path/summary/role 等 staging 模型不接受的字段。
#: 2.3.0:运行命令移交给确定性验证发现器；架构员工不再猜 test_cmd/build_cmd。
PROCEDURE_VERSION = "2.3.0"
EMPLOYEE_ID = "arch-employee"

#: 卡片骨架(PRD 41)。init 与 deepen 共用——提示词教的形状必须正是校验器认的形状,
#: 两份各写一遍的话迟早教出两种卡。
CARD_SKELETON = """\
```markdown
---
id: <功能点id>
confidence: high
---

一句话说清这个功能点在系统里扮演什么角色。

- **承重不变量**:改这里的代码时什么必须成立,精确到表达式级
  (如"`forbid` 优先于 `write`"),每条带锚点。
- **异常与降级路径**:出错走哪条路、降成什么、什么绝不静默吞掉。
- **测试证据**:哪个测试的哪条断言钉住了上面哪条行为。
- **坑点**:踩过的坑与它的出处(fix commit / 事故记录)。
```"""

#: 减法筛:什么算一张合格的卡(ADR-0011)。
CARD_DISCIPLINE = """写卡片的纪律(减法筛):

- **描述行为的句子删掉,只留必须成立的。** 功能清单(逐文件罗列"xxx.py 负责什么")
  明令禁止——代码自己就能说清它做什么;不变量才是不通读代码就读不出来的东西。
- **锚点写 `路径:行号` 或 `路径::符号名`**,基准是 Workspace 相对或模块目录相对。
  引用的路径必须真实存在——校验会逐条查存在性;行号允许漂移。不带斜杠的裸文件名
  只在带了行号/符号锚点时才被当作引用查验;提到运行态文件名(如 `task.json`)的
  散文不受影响。
- **没有行数要求。** 薄而真胜过厚而编;拿不准的标低 confidence,不要凑字数。
- 不变量藏在两处:fix/revert 的提交历史(它蕴含的 never-again 规则),和测试里
  奇怪而具体的断言(为什么这里要断言 X ≠ Y)。先挖这两处,再动笔。"""


def build_prompt(
    project_map: ProjectMap,
    workspace_root: Path,
    focus: str = "",
    output_dir: Path | None = None,
) -> str:
    """组装架构员工的上下文包。

    重跑时把已有地图整个注入——员工产出的是增量而非全量覆盖,它得先知道现在有什么。

    `focus` 给出模块 id 时,这一趟**只读那一个模块**,但地图仍然整份注入:跨模块契约要靠
    "知道别人是谁"才报得准,只给自己那一段的话,`depends_on` 与 `consumers` 只能靠猜。

    `output_dir` 是这次 Job 的产物目录。**必须写进提示词**——staging 树与小票都落在
    它底下,而"产物该写到哪"若只存在于编排器的脑子里,员工只能猜一个。
    """
    staging = output_dir_display(output_dir, workspace_root) + "/" + STAGING_DIR
    only = (
        f"\n\n## 这一趟只读 `{focus}`\n\n"
        f"**staging 里的模块只允许出现 `{focus}` 这一个。** 其余模块由各自的作业负责,\n"
        f"你多写的那些会被校验直接拒绝。\n"
        f"`interfaces.yaml` 照写:契约是跨模块的,两侧各报一次会在汇总时按 id 合并。\n"
        if focus
        else ""
    )
    known = yaml.safe_dump(
        project_map.model_dump(mode="json", by_alias=True, exclude_none=True),
        allow_unicode=True,
        sort_keys=False,
    )
    return f"""# knowledge-init

你是 AgentGenome 的架构数字员工。通读挂载在本 Workspace 下的全部业务模块代码,
产出项目认知。

## 铁律

1. **只读代码,不改任何业务文件。** 你的产出是 `{staging}/` 下的树文件与一张小票,
   不是代码改动,也不是对话里的总结——**没写成文件的认知等于没有产出**。
2. **不确定的认知标注 `confidence`(0~1)而非编造。** 低置信度会被人复核,
   编造的高置信度会误导后续所有任务。
3. **不要发明模块。** 模块的 `id` 与 `path` 已经由 init 从 `.gitmodules` 确定,
   见下方现有地图。你只补认知类字段。
4. `interfaces.yaml` 是质量要求最高的部分——它是"这次改动会不会波及别人"的唯一
   结构化依据。请仔细找出跨模块契约:HTTP/RPC 接口定义、共享 schema 文件、事件约定。
5. **每个模块要拆出功能点,而且每个功能点要么给一张卡片文件、要么给一条带理由的
   `no_card`。** 两者都不给的话这个模块的知识是不完备的,校验会把这次产出拦下来。
   "不值得写卡片"是一个合法的判断——但它必须写出理由,而不是留空。
6. **`scope` 里的每一条都必须是这次真的在磁盘上读到的文件或目录。** 源码里出现的
   路径字符串不等于那个路径本身存在——写 `scope` 之前,按拼出来的完整前缀确认一遍
   它真的在这次扫描到的文件树里。
7. **不要生成或修改 `test_cmd` / `build_cmd`。** 它们是兼容旧 Workspace 的字段，
   新的运行事实由确定性发现器从 Makefile/package.json 与锁文件写入
   `genome/gates/<模块id>.yaml`；没有标准入口时必须等待确认，不能由架构员工猜。

## 现有项目地图

```yaml
{known}```

## 产出:把树写进 staging/

在 `{staging}/` 下直接写真实文件,目录形状与 `genome/knowledge/` 完全相同。
**每个文件单独 Write,一个文件只装一件事——不要把任何文件内容塞进 JSON 字符串。**

```
{staging}/
  project-map.yaml           # 根索引补丁:只列这次读过的模块
  interfaces.yaml            # 跨模块契约与数据存储
  modules/<模块id>/map.yaml   # 模块地图:运行信息 + 功能点清单
  modules/<模块id>/overview.md         # 模块认知卡
  modules/<模块id>/features/<功能点id>.md  # 功能卡片
```

`project-map.yaml`(补丁,条目形状就是根索引的最终形态):

```yaml
modules:
  - id: <与现有地图一致>
    path: <与现有地图一致,不可改>
    summary: 一句话说清这个模块是干什么的
    map: modules/<模块id>/map.yaml
```

`modules/<模块id>/map.yaml`:

```yaml
id: <模块id>
lang: python
entrypoints: [src/app.py]
depends_on: [<其他模块id>]
confidence: 0.9
doc: genome/knowledge/modules/<模块id>/overview.md
features:
  - id: <功能点id>
    summary: 一句话
    scope: [<Workspace 根相对的 glob,带模块前缀,如 repos/order-service/src/order/**>]
    card: features/<功能点id>.md
  - id: <另一个功能点id>
    summary: 一句话
    scope: [<glob>]
    no_card: 为什么这里不值得写卡片
```

`interfaces.yaml`(形状就是契约索引的最终形态):

```yaml
interfaces:
  - id: <契约id>
    kind: http
    provider: <提供方模块id>
    consumers: [<消费方模块id>]
    schema: <Workspace 根相对的 schema 路径>
    confidence: 0.9
    description: 一句话说明这条契约
datastores:
  - id: <存储id>
    kind: postgres
    owner: <归属模块id>
    migrations: <Workspace 根相对的迁移目录>
    confidence: 0.9
    description: 一句话说明这个存储
```

`provider` / `consumers` / `owner` 都必须引用现有模块 id;`schema` 和
`migrations` 是可选字段,但一旦写了就必须是 Workspace 根相对路径。
**不要写 `name` / `path` / `summary` / `role`,这些不是契约索引的字段。**
没找到真实的契约或存储时写空列表,不要为了填满示例而编造。

`modules/<模块id>/features/<功能点id>.md`(front matter 的 `id` 必须与功能点一致):

{CARD_SKELETON}

{CARD_DISCIPLINE}

## 小票:result.json

树写完之后,在产物目录写一张**扁平的**小票(不放任何文件内容进来):

```json
{{
  "task_id": "knowledge-init",
  "producer": "arch-employee",
  "notes": ["值得一提的观察"],
  "questions": ["拿不准、需要人拍板的问题"],
  "low_confidence": ["modules/<模块id>/features/<功能点id>.md"]
}}
```

成败由编排器对 staging/ 跑确定性校验裁决,小票只进人工校对清单——
**写得再漂亮也替代不了树文件**。

## 如果上一次的产出被拒绝了

拒绝原因是**逐文件**的问题清单。staging 里已有的文件都还在:
**只修清单点名的那几个文件,已通过的文件不要重写。**

Workspace 根目录:{workspace_root}

## 路径怎么写

**`scope` 是 Workspace 根相对的,必须带模块前缀。** 业务仓挂在 `repos/<仓库名>/` 底下,
所以 `order-service` 模块里的 `src/order/app.py`,写进 `scope` 要写成
`repos/order-service/src/order/app.py`。
路由拿来匹配的变更清单就是这个基准——少了前缀的 scope 永远命中不到任何代码。

**`card` 不一样,它是相对模块目录的**(`features/<功能点id>.md`)。这两个字段基准不同,
别弄混。

**不要把源码里写的路径字符串当成那份文件本身存在的证据。** 有些常量的取值看起来
就是一条文件路径(比如某个默认配置项写的是 `genome/rules/xxx.md`),但那描述的可能
是别的东西的目录约定,不代表 `<模块前缀>/genome/rules/xxx.md` 这份文件真的在这次
要扫描的代码里存在。写进 `scope` 前,按拼出来的完整路径实际确认一遍。

上面每个模块的 `path` 就是它的前缀,照着拼。
{only}"""


def output_dir_display(output_dir: Path | None, workspace_root: Path) -> str:
    """产物目录在提示词里怎么写。能相对 Workspace 根就相对写——员工的 cwd 就是根,
    相对路径在它手里直接可用;给绝对路径的话,换台机器的录制与回放就对不上了。
    init 与 deepen 共用(两份各写一遍的话,迟早一份忘了"相对可回放"这条理由)。"""
    if output_dir is None:
        return "<产物目录>"
    try:
        return str(Path(output_dir).relative_to(workspace_root))
    except ValueError:
        return str(output_dir)


__all__ = [
    "CARD_DISCIPLINE",
    "CARD_SKELETON",
    "EMPLOYEE_ID",
    "PROCEDURE_ID",
    "PROCEDURE_VERSION",
    "RECEIPT_SCHEMA",
    "build_prompt",
]
