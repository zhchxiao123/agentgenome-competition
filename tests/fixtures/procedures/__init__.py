"""可复用的最小 Procedure 目录构造器。

四个测试文件各写一份目录构造器之后,"合法的 Procedure 长什么样"这件事就有了四个略有
出入的版本。集中一处,顺便让"各类非法形态"成为一份可读的清单。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 一个满足公共产物约束的结果。
RESULT: dict[str, Any] = {
    "task_id": "ag-1",
    "producer": "p",
    "created_at": "2026-09-01T10:00:00Z",
    "passed": True,
}

OUTPUT_SCHEMA: dict[str, Any] = {"type": "object", "required": ["task_id", "passed"]}

#: 把结果写好就退出。
#:
#: 插的是 Python 字面量而不是 JSON——后者产出的 `true` 在 Python 源码里是 NameError,
#: 而这个脚本是被当作 Python 跑的。
GOOD_SCRIPT = f"""\
import json, os, pathlib, sys
out = pathlib.Path(os.environ["AGENTGENOME_OUTPUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "result.json").write_text(json.dumps({RESULT!r}))
sys.exit(0)
"""

#: 写出不符 schema 的结果。
BAD_SCRIPT = """\
import json, os, pathlib, sys
out = pathlib.Path(os.environ["AGENTGENOME_OUTPUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "result.json").write_text(json.dumps({"nope": True}))
sys.exit(0)
"""

#: 非零退出。
FAILING_SCRIPT = "import sys\nsys.exit(3)\n"

#: hybrid 的前半段:产出中间结果给 Agent 用。
INTERMEDIATE_SCRIPT = """\
import json, os, pathlib, sys
out = pathlib.Path(os.environ["AGENTGENOME_OUTPUT_DIR"])
out.mkdir(parents=True, exist_ok=True)
(out / "intermediate.json").write_text(json.dumps({"scanned": 7}))
sys.exit(0)
"""


@dataclass(frozen=True)
class ProcedureFixture:
    """一份写到磁盘上的 Procedure。"""

    path: Path

    @property
    def manifest(self) -> Path:
        return self.path / "procedure.yaml"


def write_procedure(
    root: Path,
    procedure_id: str,
    kind: str = "deterministic",
    *,
    version: str = "1.0.0",
    summary: str = "占位",
    script: str | None = GOOD_SCRIPT,
    prompt: str | None = None,
    trigger: list[str] | None = None,
    inputs_schema: dict[str, Any] | None = None,
    outputs_artifacts: list[str] | None = None,
    schema_ref: str | None = "schemas/out.json",
    required_cmds: list[str] | None = None,
    runtimes: list[str] | None = None,
    on_fail: str | None = None,
    extra_manifest: str = "",
) -> ProcedureFixture:
    """写出一个 Procedure 目录。默认形态是合法的最小确定性 Procedure。"""
    directory = root / procedure_id
    directory.mkdir(parents=True, exist_ok=True)

    lines = [f"id: {procedure_id}", f"version: {version}", f"summary: {summary}", f"kind: {kind}"]
    if trigger is not None:
        lines.append("trigger:\n  states: [" + ", ".join(trigger) + "]")
    if inputs_schema is not None:
        lines.append("inputs:\n  schema: " + json.dumps(inputs_schema))
    outputs = []
    if outputs_artifacts is not None:
        outputs.append("  artifacts: [" + ", ".join(outputs_artifacts) + "]")
    if schema_ref is not None:
        outputs.append(f"  schema_ref: {schema_ref}")
    if outputs:
        lines.append("outputs:\n" + "\n".join(outputs))
    if required_cmds is not None:
        lines.append("tools:\n  required_cmds: [" + ", ".join(required_cmds) + "]")
    if runtimes is not None:
        lines.append("compat:\n  runtimes: [" + ", ".join(runtimes) + "]")
    if on_fail is not None:
        lines.append(f"failure:\n  on_fail: {on_fail}")
    manifest = "\n".join(lines) + "\n" + extra_manifest
    directory.joinpath("procedure.yaml").write_text(manifest, encoding="utf-8")

    if schema_ref is not None:
        target = directory / schema_ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
    if script is not None:
        scripts = directory / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "run.py").write_text(script, encoding="utf-8")
    if prompt is not None:
        (directory / "prompt.md").write_text(prompt, encoding="utf-8")

    return ProcedureFixture(directory)


def write_broken_procedure(root: Path, procedure_id: str = "broken") -> ProcedureFixture:
    """版本号不是 semver——注册表应该跳过它并记下原因。"""
    return write_procedure(root, procedure_id, version="完全不是 semver")


def write_agentic_procedure(root: Path, procedure_id: str, prompt: str = "干活\n", **kwargs: Any):
    return write_procedure(root, procedure_id, "agentic", script=None, prompt=prompt, **kwargs)


def write_hybrid_procedure(
    root: Path, procedure_id: str, prompt: str = "诊断一下\n", **kwargs: Any
):
    kwargs.setdefault("script", INTERMEDIATE_SCRIPT)
    return write_procedure(root, procedure_id, "hybrid", prompt=prompt, **kwargs)
