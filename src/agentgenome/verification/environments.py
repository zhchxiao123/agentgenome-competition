"""执行环境 Adapter seam 与 composition root。"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from agentgenome.verification.models import EnvironmentRef


class AdapterUnavailable(RuntimeError):
    """规格引用的环境无法在当前宿主机准备。"""


@dataclass(frozen=True)
class PrepareContext:
    module_root: Path
    scratch_root: Path
    base_env: Mapping[str, str]


@dataclass(frozen=True)
class PreparedEnvironment:
    env: Mapping[str, str]
    setup: tuple[tuple[str, ...], ...] = ()


class EnvironmentAdapter(Protocol):
    id: str

    @property
    def proposal_guidance(self) -> str: ...

    def validate(self, reference: EnvironmentRef, module_root: Path) -> None: ...

    def prepare(
        self, reference: EnvironmentRef, context: PrepareContext
    ) -> AbstractContextManager[PreparedEnvironment]: ...


class AdapterRegistry:
    """一个 composition root 显式装配的不可变 Adapter 集合。"""

    def __init__(self, adapters: tuple[EnvironmentAdapter, ...]) -> None:
        by_id = {adapter.id: adapter for adapter in adapters}
        if len(by_id) != len(adapters):
            raise ValueError("执行环境 Adapter id 重复")
        self._by_id = by_id

    def require(self, adapter_id: str) -> EnvironmentAdapter:
        try:
            return self._by_id[adapter_id]
        except KeyError as error:
            raise AdapterUnavailable(f"未注册执行环境 Adapter: {adapter_id}") from error

    def validate(self, reference: EnvironmentRef, module_root: Path) -> None:
        self.require(reference.adapter).validate(reference, module_root)

    def proposal_guidance(self) -> str:
        return "\n".join(
            f"  - `{adapter_id}`: {adapter.proposal_guidance}"
            for adapter_id, adapter in sorted(self._by_id.items())
        )


class HostProcessAdapter:
    id = "host.process"
    proposal_guidance = "不接受 options"

    def validate(self, reference: EnvironmentRef, module_root: Path) -> None:
        if reference.options:
            raise AdapterUnavailable("host.process 不接受 options")

    @contextmanager
    def prepare(
        self, reference: EnvironmentRef, context: PrepareContext
    ) -> Iterator[PreparedEnvironment]:
        self.validate(reference, context.module_root)
        yield PreparedEnvironment(env=dict(context.base_env))


class TrustedHostAdapter:
    """运行平台门禁；不继承员工虚拟环境里可伪造的同名工具。"""

    id = "host.trusted"
    proposal_guidance = "不接受 options"

    def validate(self, reference: EnvironmentRef, module_root: Path) -> None:
        if reference.options:
            raise AdapterUnavailable("host.trusted 不接受 options")

    @contextmanager
    def prepare(
        self, reference: EnvironmentRef, context: PrepareContext
    ) -> Iterator[PreparedEnvironment]:
        self.validate(reference, context.module_root)
        environment = dict(context.base_env)
        employee_environment = environment.pop("VIRTUAL_ENV", None)
        environment.pop("PYTHONPATH", None)
        environment.pop("NODE_PATH", None)
        environment["PATH"] = _clean_path(
            environment.get("PATH", ""), employee_environment
        )
        yield PreparedEnvironment(env=environment)


class PythonUvAdapter:
    id = "python.uv"
    proposal_guidance = "`project_file`、`lockfile` 都是必填字符串"

    def validate(self, reference: EnvironmentRef, module_root: Path) -> None:
        _reject_unknown_options(reference, {"project_file", "lockfile"})
        _required_declared_file(reference, "project_file", module_root)
        _required_declared_file(reference, "lockfile", module_root)

    @contextmanager
    def prepare(
        self, reference: EnvironmentRef, context: PrepareContext
    ) -> Iterator[PreparedEnvironment]:
        self.validate(reference, context.module_root)
        environment = dict(context.base_env)
        employee_environment = environment.pop("VIRTUAL_ENV", None)
        clean_path = _clean_path(environment.get("PATH", ""), employee_environment)
        safe_uv = shutil.which("uv", path=clean_path)
        if safe_uv is None:
            raise AdapterUnavailable("命令不存在: uv。员工虚拟环境之外没有可信版本。")

        environment["PATH"] = clean_path
        # 不设置 UV_PROJECT_ENVIRONMENT。Makefile 可能在多个子项目目录里调用 uv；
        # 一个全局 venv 会把这些项目错误地揉进同一环境。任务 worktree 自身就是隔离边界，
        # uv 应按每次调用的 cwd 选择各自的项目环境。
        environment.pop("UV_PROJECT_ENVIRONMENT", None)
        environment["UV_CACHE_DIR"] = str(context.scratch_root / "uv-cache")
        environment["PYTHONNOUSERSITE"] = "1"
        yield PreparedEnvironment(env=environment)


NodeManager = Literal["npm", "pnpm", "yarn"]


class NodePackageAdapter:
    def __init__(self, manager: NodeManager) -> None:
        self.manager = manager
        self.id = f"node.{manager}"

    @property
    def proposal_guidance(self) -> str:
        base = "`manifest`、`lockfile` 都是必填字符串"
        if self.manager != "yarn":
            return base
        return f"{base}；`generation` 必填，值只能是 `classic` 或 `berry`"

    def validate(self, reference: EnvironmentRef, module_root: Path) -> None:
        allowed = {"manifest", "lockfile"}
        if self.manager == "yarn":
            allowed.add("generation")
        _reject_unknown_options(reference, allowed)
        _required_declared_file(reference, "manifest", module_root)
        _required_declared_file(reference, "lockfile", module_root)
        if self.manager == "yarn":
            generation = _required_option(reference, "generation")
            if generation not in {"classic", "berry"}:
                raise AdapterUnavailable(
                    "node.yarn 的 generation 必须是 classic 或 berry"
                )

    @contextmanager
    def prepare(
        self, reference: EnvironmentRef, context: PrepareContext
    ) -> Iterator[PreparedEnvironment]:
        self.validate(reference, context.module_root)
        environment = dict(context.base_env)
        employee_environment = environment.pop("VIRTUAL_ENV", None)
        environment.pop("NODE_PATH", None)
        clean_path = _clean_path(environment.get("PATH", ""), employee_environment)
        if shutil.which(self.manager, path=clean_path) is None:
            raise AdapterUnavailable(
                f"命令不存在: {self.manager}。员工环境之外没有可信版本。"
            )
        environment["PATH"] = clean_path
        environment["npm_config_cache"] = str(context.scratch_root / "npm-cache")
        setup: tuple[str, ...]
        if self.manager == "yarn":
            generation = _required_option(reference, "generation")
            setup = (
                "yarn",
                "install",
                "--frozen-lockfile" if generation == "classic" else "--immutable",
            )
        else:
            setup = {
                "npm": ("npm", "ci"),
                "pnpm": ("pnpm", "install", "--frozen-lockfile"),
            }[self.manager]
        yield PreparedEnvironment(env=environment, setup=(setup,))


def default_registry() -> AdapterRegistry:
    return AdapterRegistry(
        (
            HostProcessAdapter(),
            TrustedHostAdapter(),
            PythonUvAdapter(),
            NodePackageAdapter("npm"),
            NodePackageAdapter("pnpm"),
            NodePackageAdapter("yarn"),
        )
    )


def process_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    if overrides is not None:
        environment.update(overrides)
    return environment


def _required_option(reference: EnvironmentRef, name: str) -> str:
    value = reference.options.get(name)
    if not isinstance(value, str) or not value:
        raise AdapterUnavailable(f"{reference.adapter} 缺少字符串 option: {name}")
    return value


def _reject_unknown_options(reference: EnvironmentRef, allowed: set[str]) -> None:
    unknown = sorted(set(reference.options) - allowed)
    if unknown:
        raise AdapterUnavailable(
            f"{reference.adapter} 包含未知 options: {', '.join(unknown)}"
        )


def _required_declared_file(
    reference: EnvironmentRef, name: str, module_root: Path
) -> str:
    relative = _required_option(reference, name)
    root = module_root.resolve()
    if Path(relative).is_absolute():
        raise AdapterUnavailable(
            f"{reference.adapter} option {name} 必须是模块内相对路径"
        )
    declared = (root / relative).resolve()
    if not declared.is_relative_to(root):
        raise AdapterUnavailable(
            f"{reference.adapter} option {name} 必须是模块内相对路径"
        )
    if not declared.is_file():
        raise AdapterUnavailable(f"{reference.adapter} 缺少声明文件: {relative}")
    return relative


def _clean_path(path: str, employee_environment: str | None) -> str:
    if not employee_environment:
        return path
    employee_bin = (Path(employee_environment).resolve() / "bin")
    return os.pathsep.join(
        entry
        for entry in path.split(os.pathsep)
        if entry and Path(entry).resolve() != employee_bin
    )


__all__ = [
    "AdapterRegistry",
    "AdapterUnavailable",
    "EnvironmentAdapter",
    "NodePackageAdapter",
    "PythonUvAdapter",
    "PrepareContext",
    "PreparedEnvironment",
    "TrustedHostAdapter",
    "default_registry",
    "process_environment",
]
