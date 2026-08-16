"""项目地图的数据模型。

项目地图是员工"带着认知上岗"的入口:结构化优先、正文引用 Markdown。
全部字段用 pydantic 定义并禁止未知字段——拼写错误静默失效比报错难查得多。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid")


class ProjectInfo(BaseModel):
    model_config = _STRICT

    name: str
    summary: str = ""


class Module(BaseModel):
    """一个业务模块,对应一个 Git 子模块或子目录。"""

    model_config = _STRICT

    id: str
    path: str
    lang: str | None = None
    summary: str = ""
    entrypoints: list[str] = Field(default_factory=list)
    test_cmd: str | None = None
    build_cmd: str | None = None
    #: 在集成测试环境里跑这个模块用例的命令。没声明的模块不产出集成用例。
    itest_cmd: str | None = None
    junit_xml_path: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    doc: str | None = None
    confidence: float | None = None


class Interface(BaseModel):
    """跨模块契约。触碰它的 schema 是集成测试触发判定的主要依据。"""

    # `schema` 在 pydantic 上是保留名,存储时改叫 schema_path,对外仍是 `schema`。
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    kind: str
    provider: str
    consumers: list[str] = Field(default_factory=list)
    schema_path: str | None = Field(default=None, alias="schema")
    confidence: float | None = None
    #: 与 `Module.summary` 同一个理由:知识初始化的真实产出里,架构员工几乎总会给
    #: 跨模块契约配一句说明("库存预占接口"这类)。此前这里没有对应字段,`extra="forbid"`
    #: 会把它当成拼写错误直接拒收——而那时 Job 已经真的跑完、token 已经真的花了,
    #: 只是本地这一步解析失败,报错读起来像是配置问题,实际是 schema 少了一个字段。
    description: str = ""


class Datastore(BaseModel):
    """数据存储及其迁移目录归属。触碰迁移目录会被判为高风险。"""

    model_config = _STRICT

    id: str
    kind: str
    owner: str
    migrations: str | None = None
    confidence: float | None = None
    #: 同 `Interface.description`。
    description: str = ""


class ProjectMap(BaseModel):
    model_config = _STRICT

    version: int = 1
    updated_at: datetime | None = None
    project: ProjectInfo
    modules: list[Module] = Field(default_factory=list)
    interfaces: list[Interface] = Field(default_factory=list)
    datastores: list[Datastore] = Field(default_factory=list)

    def module(self, module_id: str) -> Module:
        for module in self.modules:
            if module.id == module_id:
                return module
        raise KeyError(f"项目地图中没有模块: {module_id}")

    def module_ids(self) -> set[str]:
        return {module.id for module in self.modules}

    def module_paths(self) -> dict[str, str]:
        """模块 id → 挂载点。**身份到位置的翻译只有这一处说得出来。**

        此前门禁、集测判定、同仓串行锁各建一遍这个字典;抄得越多,哪天 path 的写法变了
        (比如尾斜杠)就越可能只改好其中几处。
        """
        return {module.id: module.path for module in self.modules}

    def mount_paths(self, module_ids: Sequence[str]) -> list[str]:
        """这几个模块的挂载点,按给出顺序,去重。

        **认不出的 id 直接丢掉,不报错。** 调用方是权限收窄:丢掉的方向是**更窄**,
        而更窄的失败是可恢复的(员工撞上越权、拿到失败报告、下一轮改)。反过来把认不出的
        id 当通配符才是危险的。计划里一个模块都认不出时,收窄后的可写集合为空,
        那一层自有拒绝派发的判断,不必在这里抢着报错。
        """
        known = self.module_paths()
        found = [known[item] for item in module_ids if item in known]
        return list(dict.fromkeys(found))
