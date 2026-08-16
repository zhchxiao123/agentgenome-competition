# AgentGenome

AgentGenome 是一个版本化原生的 AI 研发协同底座。它把项目知识、架构规则、执行流程和
数字员工配置作为可版本化资产保存，并通过任务状态机协调需求分析、代码开发、质量门禁、
集成测试、人工审批和经验回流。

项目提供 CLI、REST API 和 Web 控制台，支持 Claude Code、Qwen Code 以及 AgentTeams
等可插拔运行时。

## 主要能力

- 以 Git Workspace 隔离任务改动，保护主分支并保留完整审计链路。
- 通过数字员工与 Procedure 组合编排需求分析、开发、测试和评审工作。
- 统一运行单元测试、构建、安全扫描和集成测试等质量门禁。
- 将项目知识、架构规则和经验卡片沉淀到 `genome/`，随代码持续演进。
- 提供任务、需求、人工待办、运行记录和配置管理的 REST API 与 Web 控制台。
- 支持本地 CLI Agent 和 AgentTeams 平台运行时按员工混合配置。

## 环境要求

- Python 3.11 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Git
- Node.js 22 LTS 或 24 LTS（运行 Web 控制台时需要）
- 可选：Docker、gitleaks，以及配置好的 Claude Code 或 Qwen Code CLI

## 安装

```bash
git clone <repository-url>
cd agentgenome-competition
uv sync --all-extras
```

安装完成后可以查看 CLI：

```bash
uv run agctl --help
```

## 创建 Workspace

AgentGenome 的 Workspace 保存治理配置、项目基因组和业务仓库指针。业务仓库必须至少有
一个 Git 提交。

```bash
uv run agctl init ./workspace \
  --local-only \
  --name demo \
  --repo https://github.com/example/backend.git
```

可以重复传入 `--repo` 挂载多个业务仓库：

```bash
uv run agctl init ./workspace \
  --local-only \
  --name mall \
  --repo https://github.com/example/order-service.git \
  --repo https://github.com/example/inventory-service.git
```

初始化后，运行时、预算、并发度和平台参数位于 `workspace/agentgenome.yaml`。

## 启动后端

单项目模式：

```bash
uv run agctl serve --workspace ./workspace --host 127.0.0.1 --port 8080
```

服务启动后，REST API 位于 `http://127.0.0.1:8080`。如果需要同时管理多个 Workspace，
可以先注册项目，再以注册表模式启动：

```bash
uv run agctl workspace register demo ./workspace
uv run agctl serve --host 127.0.0.1 --port 8080
```

## 启动 Web 控制台

另开一个终端：

```bash
cd web
npm ci
npm run dev
```

打开 Vite 输出的地址（默认 `http://127.0.0.1:5173`）。开发服务器会把 API 请求代理到
`http://127.0.0.1:8080`。后端使用其他地址时，可以这样启动：

```bash
AGENTGENOME_API=http://127.0.0.1:9000 npm run dev
```

## 提交并推进任务

```bash
uv run agctl task submit \
  --workspace ./workspace \
  --requirement "为订单查询接口增加分页能力" \
  --json

uv run agctl task run <task-id> \
  --workspace ./workspace \
  --steps 5

uv run agctl task status <task-id> \
  --workspace ./workspace
```

也可以直接通过 Web 控制台创建、观察和处理任务。

## 测试与构建

后端测试、静态检查与类型检查：

```bash
uv run pytest
uv run ruff check src tests
uv run mypy src
```

前端测试与生产构建：

```bash
cd web
npm ci
npm test
npm run build
```

## 文档

- [系统设计](docs/AgentGenome.md)
- [AgentTeams 运行时](docs/agentteams.md)
- [运行时能力矩阵](docs/runtime-matrix.md)
- [架构决策记录](docs/adr)
- [OpenAPI 规范](docs/openapi.json)

## License

本项目采用 [Apache License 2.0](LICENSE)。
