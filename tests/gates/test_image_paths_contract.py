"""FR-IMG-12 (#257, #259): the image generation path never imports the safety context or the
image path's canonical-age reader.

The import-linter contract ``images-generation-path-has-no-ages`` forbids
``orchestrator.images.adapters``, ``orchestrator.images.port`` and
``orchestrator.images.prompt_builder`` (#259) and ``orchestrator.images.request`` (#797) from
importing ``core.images.safety`` and ``core.catalog.ages``, and (#797) ``core.images.store`` and
``orchestrator.images.jobs``, which hold an ``ImageJob`` and its ``safety_context`` (direct
imports). These tests check that the contract exists as written, that the real tree keeps it,
and that it bites: a temporary copy of the package whose OpenRouter adapter imports the age
reader breaks it, so does one whose prompt builder imports the safety context, and so does one
whose request builder imports ``ImageJob`` (even under ``TYPE_CHECKING``).

#797 (#257 AC 5): the adapter's ``ImageRequest`` is built only in ``orchestrator.images.request``,
from ``RequestInputs``. A structural walk checks that neither the builders' signatures nor the
narrowed value reach a type of a forbidden module or a field named for the safety context or an
age, and a source scan checks that nothing else in the engine constructs an ``ImageRequest``.
"""

from __future__ import annotations

import ast
import dataclasses
import shutil
import subprocess
import sys
import tomllib
import typing
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from epistrel.core.images.store import ImageJob
from epistrel.orchestrator.images import request as request_module
from epistrel.orchestrator.images.request import (
    RequestInputs,
    build_image_request,
    resend_request,
)
from tests.conftest import REPO_ROOT

LINT_IMPORTS = Path(sys.executable).parent / "lint-imports"
CONTRACT = "images-generation-path-has-no-ages"
SOURCES = [
    "epistrel.orchestrator.images.adapters",
    "epistrel.orchestrator.images.port",
    "epistrel.orchestrator.images.prompt_builder",
    "epistrel.orchestrator.images.request",
]
FORBIDDEN = [
    "epistrel.core.images.safety",
    "epistrel.core.catalog.ages",
    "epistrel.core.images.store",
    "epistrel.orchestrator.images.jobs",
]
SRC = REPO_ROOT / "src" / "epistrel"
BUILDERS: list[Callable[..., Any]] = [build_image_request, resend_request]
AGE_WORDS = {"safety", "age", "ages"}
"""A field or parameter one of whose ``_``-separated words is one of these is a handle to true
ages (``safety_context``, ``true_age``; not ``reference_images``)."""
REQUEST_BUILDER_MODULES = {
    SRC / "orchestrator" / "images" / "request.py",
    SRC / "orchestrator" / "images" / "port.py",
}
"""Where ``ImageRequest`` may be constructed: its builder, and its own module (validators)."""


def _contract() -> dict[str, object]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    contracts = {c["name"]: c for c in config["tool"]["importlinter"]["contracts"]}
    found: dict[str, object] = contracts[CONTRACT]
    return found


def _lint(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(LINT_IMPORTS), *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=cwd,
    )


def test_the_contract_exists_as_written() -> None:
    contract = _contract()
    assert contract["type"] == "forbidden"
    assert contract["source_modules"] == SOURCES
    assert contract["forbidden_modules"] == FORBIDDEN
    assert contract["allow_indirect_imports"] is True


def test_the_real_tree_keeps_the_contract() -> None:
    proc = _lint(REPO_ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"{CONTRACT} KEPT" in proc.stdout


def test_the_contract_bites_on_a_copy_with_a_forbidden_import(tmp_path: Path) -> None:
    """A copy of the package whose OpenRouter adapter imports ``core.catalog.ages`` breaks the
    contract; the same copy untouched keeps it (the complement)."""
    package = tmp_path / "epistrel"
    shutil.copytree(SRC, package, ignore=shutil.ignore_patterns("__pycache__"))
    contract = _contract()
    listed = ", ".join(f'"{m}"' for m in contract["forbidden_modules"])  # type: ignore[attr-defined]
    sources = ", ".join(f'"{m}"' for m in contract["source_modules"])  # type: ignore[attr-defined]
    (tmp_path / "importlinter.toml").write_text(
        "[tool.importlinter]\n"
        'root_package = "epistrel"\n\n'
        "[[tool.importlinter.contracts]]\n"
        f'name = "{CONTRACT}"\n'
        'type = "forbidden"\n'
        f"source_modules = [{sources}]\n"
        f"forbidden_modules = [{listed}]\n"
        "allow_indirect_imports = true\n"
    )
    kept = _lint(tmp_path, "--config", "importlinter.toml", "--no-cache")
    assert kept.returncode == 0, kept.stdout + kept.stderr
    adapter = package / "orchestrator" / "images" / "adapters" / "openrouter.py"
    adapter.write_text(
        adapter.read_text(encoding="utf-8") + "\nfrom epistrel.core.catalog import ages  # noqa\n",
        encoding="utf-8",
    )
    broken = _lint(tmp_path, "--config", "importlinter.toml", "--no-cache")
    assert broken.returncode == 1, broken.stdout + broken.stderr
    assert f"{CONTRACT} BROKEN" in broken.stdout
    assert "epistrel.core.catalog.ages" in broken.stdout


def test_the_contract_bites_on_the_prompt_builder(tmp_path: Path) -> None:
    """#259: a copy whose prompt builder imports the safety context breaks the contract."""
    package = tmp_path / "epistrel"
    shutil.copytree(SRC, package, ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "importlinter.toml").write_text(
        "[tool.importlinter]\n"
        'root_package = "epistrel"\n\n'
        "[[tool.importlinter.contracts]]\n"
        f'name = "{CONTRACT}"\n'
        'type = "forbidden"\n'
        f"source_modules = [{', '.join(repr(m) for m in SOURCES)}]\n"
        f"forbidden_modules = [{', '.join(repr(m) for m in FORBIDDEN)}]\n"
        "allow_indirect_imports = true\n"
    )
    builder = package / "orchestrator" / "images" / "prompt_builder.py"
    builder.write_text(
        builder.read_text(encoding="utf-8") + "\nfrom epistrel.core.images import safety  # noqa\n",
        encoding="utf-8",
    )
    broken = _lint(tmp_path, "--config", "importlinter.toml", "--no-cache")
    assert broken.returncode == 1, broken.stdout + broken.stderr
    assert f"{CONTRACT} BROKEN" in broken.stdout
    assert "epistrel.core.images.safety" in broken.stdout


def _copy_with_contract(tmp_path: Path) -> Path:
    """A copy of the package with the contract as ``pyproject.toml`` writes it; the copy's root."""
    package = tmp_path / "epistrel"
    shutil.copytree(SRC, package, ignore=shutil.ignore_patterns("__pycache__"))
    contract = _contract()
    listed = ", ".join(f'"{m}"' for m in contract["forbidden_modules"])  # type: ignore[attr-defined]
    sources = ", ".join(f'"{m}"' for m in contract["source_modules"])  # type: ignore[attr-defined]
    (tmp_path / "importlinter.toml").write_text(
        "[tool.importlinter]\n"
        'root_package = "epistrel"\n\n'
        "[[tool.importlinter.contracts]]\n"
        f'name = "{CONTRACT}"\n'
        'type = "forbidden"\n'
        f"source_modules = [{sources}]\n"
        f"forbidden_modules = [{listed}]\n"
        "allow_indirect_imports = true\n"
    )
    return package


def test_the_contract_bites_on_the_request_builder(tmp_path: Path) -> None:
    """#797: a copy whose request builder imports ``ImageJob`` (from ``core.images.store``, under
    ``TYPE_CHECKING``) breaks the contract, and so does one importing ``orchestrator.images.jobs``;
    the untouched copy keeps it (the complement)."""
    package = _copy_with_contract(tmp_path)
    kept = _lint(tmp_path, "--config", "importlinter.toml", "--no-cache")
    assert kept.returncode == 0, kept.stdout + kept.stderr
    builder = package / "orchestrator" / "images" / "request.py"
    original = builder.read_text(encoding="utf-8")
    for line, forbidden in [
        (
            "\nif TYPE_CHECKING:\n    from epistrel.core.images.store import ImageJob  # noqa\n",
            "epistrel.core.images.store",
        ),
        (
            "\nfrom epistrel.orchestrator.images import jobs  # noqa\n",
            "epistrel.orchestrator.images.jobs",
        ),
    ]:
        header = "from typing import TYPE_CHECKING\n" if "TYPE_CHECKING" in line else ""
        builder.write_text(header + original + line, encoding="utf-8")
        broken = _lint(tmp_path, "--config", "importlinter.toml", "--no-cache")
        assert broken.returncode == 1, broken.stdout + broken.stderr
        assert f"{CONTRACT} BROKEN" in broken.stdout
        assert forbidden in broken.stdout


def _types_in(hint: object) -> Iterator[type]:
    """Every class a type hint names, through unions, generics and aliases."""
    if isinstance(hint, typing.TypeAliasType):
        yield from _types_in(hint.__value__)
        return
    if isinstance(hint, type):
        yield hint
    for arg in typing.get_args(hint):
        yield from _types_in(arg)
    origin = typing.get_origin(hint)
    if isinstance(origin, type):
        yield origin


def _fields(cls: type) -> dict[str, object]:
    """A dataclass's or a pydantic model's fields and their resolved hints; ``{}`` otherwise."""
    if dataclasses.is_dataclass(cls) or (isinstance(cls, type) and issubclass(cls, BaseModel)):
        return dict(typing.get_type_hints(cls))
    return {}


def _age_handles(builder: Callable[..., Any]) -> list[str]:
    """What in a builder's signature reaches true ages: a parameter or field named for the
    safety context or an age, or a type defined in a module the contract forbids."""
    found: list[str] = []
    seen: set[type] = set()

    def walk(name: str, hint: object, path: str) -> None:
        if AGE_WORDS & set(name.lower().split("_")):
            found.append(f"{path}: named {name!r}")
        for cls in _types_in(hint):
            module = getattr(cls, "__module__", "")
            if any(module == m or module.startswith(f"{m}.") for m in FORBIDDEN):
                found.append(f"{path}: type {module}.{cls.__qualname__}")
            if cls in seen:
                continue
            seen.add(cls)
            for field_name, field_hint in _fields(cls).items():
                walk(field_name, field_hint, f"{path}.{field_name}")

    for name, hint in typing.get_type_hints(builder).items():
        walk(name, hint, f"{builder.__name__}({name})")
    return found


def test_the_request_builder_has_no_handle_to_ages() -> None:
    """#797: ``build_image_request`` and ``resend_request`` take only ``RequestInputs``, the
    configured params and a snapshot; nothing they accept or return names the safety context,
    an age or a type of a forbidden module (``ImageJob``, ``SafetyContext``)."""
    assert {f.name for f in dataclasses.fields(RequestInputs)} == {
        "prompt",
        "negative_prompt",
        "size",
        "aspect_ratio",
        "quality",
        "n",
        "reference_image_ids",
    }
    for builder in BUILDERS:
        assert builder.__module__ == request_module.__name__
        assert _age_handles(builder) == []


def _takes_the_job(job: ImageJob, seed: int) -> None: ...


@dataclasses.dataclass
class _Leaky:
    prompt: str
    safety_context: dict[str, Any] | None


def _takes_leaky(inputs: _Leaky | None) -> None: ...


def test_the_walk_bites_on_a_builder_that_takes_the_job() -> None:
    """The complement: a builder that takes the ``ImageJob`` (920dba7's ``_image_request``), or a
    narrowed value that kept ``safety_context``, is flagged."""

    job_found = _age_handles(_takes_the_job)
    assert any("type epistrel.core.images.store.ImageJob" in f for f in job_found), job_found
    assert any("safety_context" in f for f in job_found), job_found
    leaky_found = _age_handles(_takes_leaky)
    assert leaky_found == ["_takes_leaky(inputs).safety_context: named 'safety_context'"]


def _names_image_request(node: ast.expr) -> bool:
    """Whether an expression is ``ImageRequest`` or ``<module>.ImageRequest``."""
    return (isinstance(node, ast.Name) and node.id == "ImageRequest") or (
        isinstance(node, ast.Attribute) and node.attr == "ImageRequest"
    )


def _constructions(tree: ast.AST) -> list[int]:
    """The lines where a module calls ``ImageRequest(...)`` or ``ImageRequest.<method>(...)``."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        names = [node.func]
        if isinstance(node.func, ast.Attribute):
            names.append(node.func.value)
        if any(_names_image_request(n) for n in names):
            lines.append(node.lineno)
    return lines


def test_only_the_request_builder_constructs_an_image_request() -> None:
    """#797: no module of the engine but ``orchestrator.images.request`` (and ``port``, which
    defines it) builds an ``ImageRequest``, so the contract covers every builder."""
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if path not in REQUEST_BUILDER_MODULES
        and (lines := _constructions(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert offenders == {}


def test_the_scan_bites_on_a_builder_outside_the_contract() -> None:
    """The complement: 920dba7's shapes in ``jobs.py`` (a direct construction and a re-send's
    ``model_validate``) are both found, and so is a module-qualified one."""
    source = (
        "def _image_request(job, seed, params):\n"
        "    return ImageRequest(prompt=job.prompt, seed=seed)\n"
        "def _resend(attempt, job):\n"
        "    return port.ImageRequest.model_validate({**attempt.request, 'prompt': job.prompt})\n"
        "def _qualified(job):\n"
        "    return port.ImageRequest(prompt=job.prompt)\n"
    )
    assert _constructions(ast.parse(source)) == [2, 4, 6]
