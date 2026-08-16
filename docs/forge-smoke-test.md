# CliForge 手工冒烟验证

`CliForge` 是对 `gh` / `glab` 的薄封装,**刻意不写自动化测试**——测它等于测 `gh`。
它的正确性由这份冒烟步骤保证。

## 何时跑

- 首次实现 `CliForge` 之后;
- 升级 `gh` / `glab` 大版本之后;
- 接入一个新的代码托管平台之后(GitHub / GitLab / Gitea 各跑一遍);
- 发布前。

自动化测试覆盖的是 `LocalForge`(见 `tests/unit/test_local_forge.py`),它跑真实 git 合并、
只是没有网络。两者共同实现 `Forge` 协议,所以这里只需要验证"CLI 这一层的参数拼装与
输出解析没写错",不需要重复验证合并语义。

## 前置

- 一个可以随便折腾的测试仓库(**不要用真实项目**);
- `gh auth status` 或 `glab auth status` 返回已登录。

## 步骤

```bash
export SMOKE=/tmp/agentgenome-forge-smoke
rm -rf "$SMOKE" && git clone <你的测试仓库> "$SMOKE" && cd "$SMOKE"

git switch -c smoke/agentgenome
echo "forge smoke $(date -u +%FT%TZ)" > smoke.txt
git add smoke.txt && git commit -m "chore: forge 冒烟"
git push -u origin smoke/agentgenome
```

然后在 Python 里逐条走一遍:

```python
from pathlib import Path
from agentgenome.space.cli_forge import CliForge

forge = CliForge(host="github")   # gitlab / gitea 时改这里
repo = Path("/tmp/agentgenome-forge-smoke")

forge.preflight()                                     # ① 不抛异常
pr = forge.open_pr(repo, head="smoke/agentgenome",
                   base="main", title="forge 冒烟", body="可以直接关掉")
print(pr)                                             # ② number 是正整数,不是 0
print(forge.pr_status(pr))                            # ③ state 为 OPEN,title 对得上
print(forge.is_protected(repo, "main"))               # ④ 与平台上的保护设置一致
rev = forge.merge_pr(pr)                              # ⑤ 平台上 PR 变为已合并
print(rev, forge.pr_status(pr))                       # ⑥ state 为 MERGED,merged_rev 非空
```

## 逐条检查清单

| # | 检查点 | 期望 |
|---|---|---|
| ① | `preflight()` | CLI 已安装且已登录时不抛;把 CLI 改名后重试应抛出可读错误 |
| ② | `open_pr` 返回的编号 | 与平台页面上的 PR 编号一致 |
| ③ | `pr_status` | `state=OPEN`,`title` 与创建时一致 |
| ④ | `is_protected` | 与平台上分支保护设置一致(建议开一次保护再关掉,两种都验) |
| ⑤ | `merge_pr` | 平台上该 PR 确实变成已合并 |
| ⑥ | 合并后的 `pr_status` | `state=MERGED`,`merged_rev` 是一个真实的 commit SHA |

## 收尾

```bash
git push origin --delete smoke/agentgenome
git switch main && git branch -D smoke/agentgenome
rm -rf "$SMOKE"
```

## 记录

每次跑完在下表追加一行。

| 日期 | 平台 | CLI 版本 | 结果 | 备注 |
|---|---|---|---|---|
| | | | | |
