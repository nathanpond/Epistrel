"""check-licenses: every runtime dependency's license is Apache-2.0-compatible (invariant 2).

Records come from `pip-licenses --format=json --from=mixed` run against a runtime-only
environment; only packages in #41's runtime closure are judged. Unknown spellings normalise to
UNKNOWN, which fails — the safe direction.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.gates.deps import DepsError, closure, load_lock, normalize

UNKNOWN = "UNKNOWN"
DENY_FAMILIES = ("GPL", "LGPL", "AGPL", "SSPL", "EUPL")

# Trove classifier and free-text spellings → SPDX ids. Keys are compared lower-case.
NORMALISATION: dict[str, str] = {
    "apache software license": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "apache": "Apache-2.0",
    "mit license": "MIT",
    "mit": "MIT",
    "mit-0": "MIT-0",
    "bsd license": "BSD-3-Clause",
    "bsd": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "2-clause bsd license": "BSD-2-Clause",
    "simplified bsd license": "BSD-2-Clause",
    "bsd-2": "BSD-2-Clause",
    "0bsd": "0BSD",
    "zero-clause bsd (0bsd)": "0BSD",
    "isc license (iscl)": "ISC",
    "isc": "ISC",
    "python software foundation license": "PSF-2.0",
    "psf": "PSF-2.0",
    "psf-2.0": "PSF-2.0",
    "python-2.0": "Python-2.0",
    "zlib/libpng license": "Zlib",
    "zlib": "Zlib",
    "the unlicense (unlicense)": "Unlicense",
    "unlicense": "Unlicense",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mpl-2.0": "MPL-2.0",
    "mozilla public license 1.1 (mpl 1.1)": "MPL-1.1",
    "mozilla public license 1.0 (mpl)": "MPL-1.0",
    "gnu general public license (gpl)": "GPL-2.0-or-later",
    "gnu general public license v2 (gplv2)": "GPL-2.0-only",
    "gnu general public license v2 or later (gplv2+)": "GPL-2.0-or-later",
    "gnu general public license v3 (gplv3)": "GPL-3.0-only",
    "gnu general public license v3 or later (gplv3+)": "GPL-3.0-or-later",
    "gnu lesser general public license v2 (lgplv2)": "LGPL-2.0-only",
    "gnu lesser general public license v2 or later (lgplv2+)": "LGPL-2.0-or-later",
    "gnu lesser general public license v3 (lgplv3)": "LGPL-3.0-only",
    "gnu lesser general public license v3 or later (lgplv3+)": "LGPL-3.0-or-later",
    "gnu library or lesser general public license (lgpl)": "LGPL-2.1-or-later",
    "gnu affero general public license v3": "AGPL-3.0-only",
    "gnu affero general public license v3 or later (agplv3+)": "AGPL-3.0-or-later",
    "european union public licence 1.2 (eupl 1.2)": "EUPL-1.2",
    "european union public licence 1.1 (eupl 1.1)": "EUPL-1.1",
    "server side public license (sspl)": "SSPL-1.0",
    "artistic license": "Artistic-2.0",
    "academic free license (afl)": "AFL-3.0",
    "eclipse public license 2.0 (epl-2.0)": "EPL-2.0",
    "eclipse public license 1.0 (epl-1.0)": "EPL-1.0",
    "common development and distribution license 1.0 (cddl-1.0)": "CDDL-1.0",
    "boost software license 1.0 (bsl-1.0)": "BSL-1.0",
    "cc0 1.0 universal (cc0 1.0) public domain dedication": "CC0-1.0",
    "public domain": "Unlicense",
    "historical permission notice and disclaimer (hpnd)": "HPND",
    "w3c license": "W3C",
    "osi approved": UNKNOWN,
    "other/proprietary license": UNKNOWN,
    "dfsg approved": UNKNOWN,
    "free for non-commercial use": UNKNOWN,
    "freely distributable": UNKNOWN,
    "freeware": UNKNOWN,
    "": UNKNOWN,
    "unknown": UNKNOWN,
}
SPDX_ID_RE = re.compile(r"^[A-Za-z0-9.+-]+$")


class LicensesError(Exception):
    """Configuration or tooling problem (structural, exit 2)."""


@dataclass(frozen=True)
class LicensesConfig:
    allow: frozenset[str]
    deny: tuple[str, ...]
    exceptions: dict[str, str] = field(default_factory=dict)


def load_licenses_config(pyproject: Path) -> LicensesConfig:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LicensesError(f"cannot read {pyproject}: {exc}") from exc
    section = data.get("tool", {}).get("epistrel", {}).get("gates", {}).get("licenses")
    if not isinstance(section, dict):
        raise LicensesError(f"{pyproject}: missing [tool.epistrel.gates.licenses]")
    unknown = set(section) - {"allow", "deny", "exceptions"}
    if unknown:
        raise LicensesError(f"{pyproject}: unknown licenses keys {sorted(unknown)}")
    allow, deny = section.get("allow"), section.get("deny")
    if not isinstance(allow, list) or not allow or not all(isinstance(a, str) for a in allow):
        raise LicensesError(f"{pyproject}: licenses.allow must be a non-empty list of SPDX ids")
    if not isinstance(deny, list) or not all(isinstance(d, str) for d in deny):
        raise LicensesError(f"{pyproject}: licenses.deny must be a list of license families")
    exceptions = section.get("exceptions", {})
    if not isinstance(exceptions, dict):
        raise LicensesError(
            f"{pyproject}: licenses.exceptions must be a table of package = rationale"
        )
    for pkg, rationale in exceptions.items():
        if not isinstance(rationale, str) or not rationale.strip():
            raise LicensesError(f"{pyproject}: exception for {pkg!r} has no rationale")
    allowed = frozenset(a.lower() for a in allow)
    for fam in deny:
        if any(
            a.upper().startswith(fam.upper() + "-") or a.upper() == fam.upper() for a in allowed
        ):
            raise LicensesError(f"{pyproject}: {fam} is both allowed and denied")
    return LicensesConfig(
        allowed, tuple(d.upper() for d in deny), {normalize(k): v for k, v in exceptions.items()}
    )


def normalise_id(token: str) -> str:
    text = token.strip()
    if not text:
        return UNKNOWN
    if text.lower() in NORMALISATION:
        return NORMALISATION[text.lower()]
    if SPDX_ID_RE.match(text):
        return text  # an SPDX-looking id we do not know; allow/deny decide
    return UNKNOWN


def parse_expression(raw: str) -> list[list[str]]:
    """OR of ANDs: `GPL-2.0 OR MIT` → [[GPL-2.0], [MIT]]; `MIT AND PSF-2.0` → [[MIT, PSF-2.0]].

    `;`-joined classifier values are OR. `WITH` keeps the left-hand id. Parentheses → UNKNOWN.
    """
    if "(" in raw or ")" in raw:
        return [[UNKNOWN]]
    alternatives: list[list[str]] = []
    for alt in re.split(r"\s*;\s*|\s+OR\s+", raw.strip()):
        conj: list[str] = []
        for term in re.split(r"\s+AND\s+", alt):
            term = re.split(r"\s+WITH\s+", term)[0]
            conj.append(normalise_id(term))
        alternatives.append(conj)
    return alternatives or [[UNKNOWN]]


def denied_family(spdx: str, deny: tuple[str, ...]) -> str | None:
    upper = spdx.upper()
    for fam in sorted(deny, key=len, reverse=True):  # LGPL before GPL
        if upper == fam or upper.startswith(fam + "-") or upper.startswith(fam + "+"):
            return fam
    return None


@dataclass(frozen=True)
class Verdict:
    package: str
    license: str
    status: str  # ok | denied | not allowed | unknown | excepted
    detail: str = ""


def judge(package: str, raw: str, config: LicensesConfig) -> Verdict:
    alternatives = parse_expression(raw)
    denied: str | None = None
    for conj in alternatives:
        if all(x.lower() in config.allow for x in conj):
            return Verdict(package, raw, "ok")
    for conj in alternatives:
        for x in conj:
            denied = denied or denied_family(x, config.deny)
    if package in config.exceptions:
        if denied:
            return Verdict(package, raw, "denied", f"exception refused: {denied} family")
        return Verdict(package, raw, "excepted", config.exceptions[package])
    if denied:
        return Verdict(package, raw, "denied", denied)
    if any(x == UNKNOWN for conj in alternatives for x in conj):
        return Verdict(package, raw, "unknown")
    return Verdict(package, raw, "not allowed")


@dataclass(frozen=True)
class LicensesReport:
    findings: list[str]
    warnings: list[str]
    judged: int
    excepted: int
    ignored: int

    @property
    def ok(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        return (
            f"{self.judged} runtime packages judged, {self.excepted} excepted, "
            f"{len(self.findings)} offending "
            f"({self.ignored} installed packages outside the closure ignored)"
        )


def _via(name: str, parents: dict[str, set[str]]) -> str:
    roots: set[str] = set()
    stack, seen = [name], set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for parent in parents.get(cur, ()):
            if parent not in parents:
                roots.add("(direct)" if cur == name else cur)
            else:
                stack.append(parent)
    return ", ".join(sorted(roots))


def evaluate(
    records: list[dict[str, Any]], parents: dict[str, set[str]], config: LicensesConfig
) -> LicensesReport:
    by_name = {normalize(str(r.get("Name", ""))): str(r.get("License", "")) for r in records}
    findings: list[str] = []
    warnings: list[str] = []
    excepted = 0
    for name in sorted(parents):
        if name not in by_name:
            warnings.append(
                f"warning: {name} is in the runtime closure but not installed here (marker-gated?)"
            )
            continue
        verdict = judge(name, by_name[name], config)
        if verdict.status == "ok":
            continue
        if verdict.status == "excepted":
            excepted += 1
            continue
        reason = {
            "denied": f"denied ({verdict.detail})",
            "unknown": "UNKNOWN license",
            "not allowed": "not allowed",
        }[verdict.status]
        shown = verdict.license[:60] + ("…" if len(verdict.license) > 60 else "")
        findings.append(f"{name}: {shown!r} {reason} [invariant 2] via {_via(name, parents)}")
    for pkg in config.exceptions:
        if pkg not in parents:
            findings.append(f"stale exception: {pkg} is not in the runtime closure")
        elif (
            pkg in by_name
            and judge(pkg, by_name[pkg], LicensesConfig(config.allow, config.deny)).status == "ok"
        ):
            warnings.append(f"warning: exception for {pkg} is redundant (license already allowed)")
    ignored = len([n for n in by_name if n not in parents])
    return LicensesReport(findings, warnings, len(parents), excepted, ignored)


def sync_runtime_env(repo_root: Path, env_dir: Path) -> None:
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(env_dir)}
    uv = shutil.which("uv")
    if uv is None:
        raise LicensesError("uv is not on PATH")
    proc = subprocess.run(  # noqa: S603 - fixed argv, resolved executable
        [uv, "sync", "--frozen", "--no-dev", "--all-extras"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise LicensesError(f"uv sync for the runtime env failed:\n{proc.stderr.strip()[-800:]}")


def collect_records(env_dir: Path) -> list[dict[str, Any]]:
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    tool = shutil.which("pip-licenses")
    if tool is None:
        raise LicensesError("pip-licenses is not on PATH (installed by the dev group)")
    proc = subprocess.run(  # noqa: S603 - fixed argv, resolved executable
        [tool, "--python", str(python), "--format=json", "--from=mixed", "--with-system"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise LicensesError(f"pip-licenses failed: {proc.stderr.strip()[-800:]}")
    data = json.loads(proc.stdout)
    if not isinstance(data, list):
        raise LicensesError("pip-licenses did not return a JSON list")
    return [d for d in data if isinstance(d, dict)]


def run(
    repo_root: Path, lock: Path, pyproject: Path, env_dir: Path, *, sync: bool
) -> LicensesReport:
    config = load_licenses_config(pyproject)
    try:
        parents = closure(load_lock(lock))
    except DepsError as exc:
        raise LicensesError(str(exc)) from exc
    if sync:
        sync_runtime_env(repo_root, env_dir)
    return evaluate(collect_records(env_dir), parents, config)
