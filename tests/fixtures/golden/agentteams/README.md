# agentteams 传输的黄金素材

录自 **2026-08-11 对真实 AgentTeams 平台的一次冒烟往返**(MatrixMinio 传输,
Worker 为 Hermes 运行时的 `developer`):任务目录经 MinIO 推送、Matrix @mention
唤醒 Worker、Worker 按交付约定写回 `artifacts/result.json` / `result.md` /
`meta.json: SUCCESS`,适配器差分回收。

- 素材形状见 `agents.agentteams.recording`:一对 `job.json` / `outcome.json`。
- **消费 token 与 Matrix 令牌结构性进不了素材**(它们是传输实现的构造参数,
  不在 `TransportJob` 里),入库前仍人工过目过一遍。
- 刷新方式:开 `AGENTGENOME_RECORD=1` + `AGENTGENOME_RECORDINGS=<库>` 对真实
  平台重跑一次冒烟(参照 PRD 31 的测试计划第 4 层),人工过目后替换本目录。
- 消费者:`tests/unit/test_agentteams_golden_replay.py`——真机形状的回放,
  不起任何平台。
