"""mc 存储镜像驱动:经"真实的假 mc"驱动,真子进程、真参数、真退出码。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentgenome.agents.agentteams.mirror import MinioMirror
from agentgenome.agents.agentteams.transport import PlatformUnavailable
from tests.fixtures import fake_mc

PREFIX = "myminio/agentteams"


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "remote"
    root.mkdir()
    monkeypatch.setenv("FAKE_MC_ROOT", str(root))
    monkeypatch.setenv("FAKE_MC_PREFIX", PREFIX)
    monkeypatch.delenv("FAKE_MC_FAIL", raising=False)
    return root


def _mirror() -> MinioMirror:
    return MinioMirror(mc_argv=[sys.executable, fake_mc.__file__], prefix=PREFIX)


async def test_push_then_pull_round_trips_byte_identical(remote: Path, tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    (source / "a.py").write_text("print(1)\n", encoding="utf-8")
    (source / "sub" / "b.md").write_text("# 文档\n", encoding="utf-8")
    target = tmp_path / "back"

    mirror = _mirror()
    await mirror.push_dir(source, "tasks/ag-1-r1/workspace")
    await mirror.pull_dir("tasks/ag-1-r1/workspace", target)

    assert (target / "a.py").read_text() == "print(1)\n"
    assert (target / "sub" / "b.md").read_text() == "# 文档\n"


async def test_push_removes_stale_remote_files(remote: Path, tmp_path: Path) -> None:
    """现场以推送方为准:上一次尝试留在远端的文件不该在这一次的现场里。"""
    source = tmp_path / "src"
    source.mkdir()
    (source / "老文件.py").write_text("1\n", encoding="utf-8")
    mirror = _mirror()
    await mirror.push_dir(source, "tasks/t/workspace")
    (source / "老文件.py").unlink()
    (source / "新文件.py").write_text("2\n", encoding="utf-8")

    await mirror.push_dir(source, "tasks/t/workspace")

    assert not (remote / "tasks" / "t" / "workspace" / "老文件.py").exists()
    assert (remote / "tasks" / "t" / "workspace" / "新文件.py").read_text() == "2\n"


async def test_pulling_a_missing_dir_leaves_the_target_empty(remote: Path, tmp_path: Path) -> None:
    """远端还没有产物目录不算失败——契约校验会用清晰的话说"没有产物"。"""
    target = tmp_path / "back"

    await _mirror().pull_dir("tasks/不存在/artifacts", target)

    assert not any(target.rglob("*"))


async def test_read_file_distinguishes_missing_from_failure(remote: Path, tmp_path: Path) -> None:
    (remote / "tasks" / "ag-1-r1").mkdir(parents=True)
    (remote / "tasks" / "ag-1-r1" / "meta.json").write_text('{"status": "PENDING"}')

    mirror = _mirror()

    assert await mirror.read_file("tasks/ag-1-r1/meta.json") == '{"status": "PENDING"}'
    assert await mirror.read_file("tasks/ag-1-r1/没有的文件") is None


async def test_a_storage_failure_is_attributed_to_the_platform(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"存储挂了"要指向平台,不是员工或工序。stderr 摘要要在错误里,可定位。"""
    monkeypatch.setenv("FAKE_MC_FAIL", "AccessDenied: 凭证过期")
    source = tmp_path / "src"
    source.mkdir()

    with pytest.raises(PlatformUnavailable, match="凭证过期"):
        await _mirror().push_dir(source, "tasks/x/workspace")


def test_preflight_rejects_a_missing_mc_executable() -> None:
    with pytest.raises(PlatformUnavailable, match="mc"):
        MinioMirror(mc_argv=["/nonexistent/mc"], prefix=PREFIX).preflight()


def test_preflight_passes_for_an_available_executable(remote: Path) -> None:
    _mirror().preflight()


def test_preflight_probes_the_prefix_not_just_the_binary(
    remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """别名/桶配置错误要在启动期暴露——进了运行期会被误当成"对象还没写完"而空转。"""
    monkeypatch.setenv("FAKE_MC_ROOT", str(remote / "不存在的桶"))

    with pytest.raises(PlatformUnavailable, match="ls"):
        _mirror().preflight()
