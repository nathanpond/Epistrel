"""check-licenses: every runtime dependency's license is Apache-2.0-compatible (invariant 2).

Records come from `pip-licenses --format=json --from=mixed` run against a runtime-only
environment; only packages in #41's runtime closure are judged. Unknown spellings normalise to
UNKNOWN, which fails — the safe direction. An exception admits an UNKNOWN or not-allowed
license, never one that names a deny family (#94): by an id that starts with the family, after
any leading `LicenseRef-` or `GNU` (`GPL-3.0-only`, `GPLv3`, `GNU-GPL`, `GPL.v3`,
`LicenseRef-GPL-3.0`), or by its long name with any separators or none (`GNU Public License`,
`Server-Side Public License`, `EuropeanUnionPublicLicence`); any `Affero` is the AGPL. A GPL
qualified as another license (`Lesser GPL`, `Library GPL`, `Nethack GPL`) is that license. A
bare `GNU` names no single family, so an exception can admit it (#313). A table entry settles
its own spelling. A license text (a value spanning several lines) is judged by its title line
only, since full texts cite or bundle other licenses in their body.
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

# Trove classifier and free-text spellings → SPDX ids. Keys are compared lower-case. Every license
# classifier in trove-classifiers 2026.9.21.13 is listed (#94).
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
    "european union public licence 1.0 (eupl 1.0)": "EUPL-1.0",
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
    # The rest of the trove set, ids checked against SPDX license list 3.27.0. None is allowed;
    # a name without its version, or with no SPDX id, is UNKNOWN rather than a guess.
    "aladdin free public license (afpl)": "Aladdin",
    "attribution assurance license": "AAL",
    "blue oak model license (blueoak-1.0.0)": "BlueOak-1.0.0",
    "cea cnrs inria logiciel libre license, version 2.1 (cecill-2.1)": "CECILL-2.1",
    "cecill-b free software license agreement (cecill-b)": "CECILL-B",
    "cecill-c free software license agreement (cecill-c)": "CECILL-C",
    "cmu license (mit-cmu)": "MIT-CMU",
    "common public license": "CPL-1.0",
    "educational community license, version 2.0 (ecl-2.0)": "ECL-2.0",
    "ibm public license": "IPL-1.0",
    "intel open source license": "Intel",
    "mit no attribution license (mit-0)": "MIT-0",
    "miros license (miros)": "MirOS",
    "motosoto license": "Motosoto",
    "mulan permissive software license v2 (mulanpsl-2.0)": "MulanPSL-2.0",
    "nasa open source agreement v1.3 (nasa-1.3)": "NASA-1.3",
    "nethack general public license": "NGPL",
    "nokia open source license": "Nokia",
    "nokia open source license (nokos)": "Nokia",
    "open group test suite license": "OGTSL",
    "open software license 3.0 (osl-3.0)": "OSL-3.0",
    "postgresql license": "PostgreSQL",
    "python license (cnri python license)": "CNRI-Python",
    "qt public license (qpl)": "QPL-1.0",
    "ricoh source code public license": "RSCPL",
    "sil open font license 1.1 (ofl-1.1)": "OFL-1.1",
    "sleepycat license": "Sleepycat",
    "sun industry standards source license (sissl)": "SISSL",
    "sun public license": "SPL-1.0",
    "universal permissive license (upl)": "UPL-1.0",
    "university of illinois/ncsa open source license": "NCSA",
    "vovida software license 1.0": "VSL-1.0",
    "x.net license": "Xnet",
    "apple public source license": UNKNOWN,  # APSL-1.0 … APSL-2.0
    "eiffel forum license": UNKNOWN,  # EFL-1.0 or EFL-2.0
    "eiffel forum license (efl)": UNKNOWN,
    "gnu free documentation license (fdl)": UNKNOWN,  # GFDL-1.1 … GFDL-1.3
    "netscape public license (npl)": UNKNOWN,  # NPL-1.0 or NPL-1.1
    "zope public license": UNKNOWN,  # ZPL-1.1 … ZPL-2.1
    "mitre collaborative virtual workspace license (cvw)": UNKNOWN,  # no SPDX id
    "gust font license 1.0": UNKNOWN,
    "gust font license 2006-09-30": UNKNOWN,
    "jabber open source license": UNKNOWN,
    "repoze public license": UNKNOWN,
    "free for educational use": UNKNOWN,
    "free for home use": UNKNOWN,
    "free to use but restricted": UNKNOWN,
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

# Deny families by long name, matched against `_words` (lower case, one space between words, so
# `Server-Side Public_License` reads as `server side public license`). A name's words may also
# be joined, its version too (`ServerSide Public License`, `EuropeanUnionPublicLicenceV1.2`),
# but `licensed` is not `license`. A family counts only when it is in the deny list. `Affero`
# names only the AGPL, and `EU Public Licence` is the EUPL. `General Public License`, with or
# without `GNU`, and the misnomer `GNU Public License` are the GPL, unless Lesser, Library, Affero
# or Nethack names another license; `Lesser GPL` and `Library GPL` are the LGPL. Identifiers
# (`GPLv3`, `GPL-3.0-only`) are matched by `denied_family` instead.
_PUBLIC_LICENSE = r"public ?licen[cs]e(?=[ v\d])"
FAMILY_NAMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AGPL", re.compile(r"affero")),
    ("LGPL", re.compile(rf" (?:lesser|library) ?(?:(?:general ?)?{_PUBLIC_LICENSE}|gpl)")),
    (
        "GPL",
        re.compile(
            r"(?<! lesser)(?<! library)(?<! affero)(?<! nethack)"
            rf" (?:gnu ?(?:general ?)?|general ?){_PUBLIC_LICENSE}"
        ),
    ),
    ("EUPL", re.compile(rf" (?:european ?union|eu) ?{_PUBLIC_LICENSE}")),
    ("SSPL", re.compile(rf" server ?side ?{_PUBLIC_LICENSE}")),
)
# Runs of free text that may be ids (`GPL-3.0-only`, `GNU-GPL`, `GPL.v3`).
TOKEN_RE = re.compile(r"[A-Za-z0-9.+-]+")
# A GPL id right after one of these words, joined by spaces or `_`, is another license: `Lesser
# GPL` is the LGPL, `Affero GPL` the AGPL and `Nethack GPL` the NGPL. `Library GPL` is the LGPL
# when it starts a phrase or follows `GNU` (`LGPL (Library GPL)`), since `Python library GPL v3`
# is the GPL. Another separator ends the qualifier, so `Library: GPL v3` is the GPL too.
GPL_QUALIFIER_RE = re.compile(
    r"(?:(?<![a-z])(?:lesser|affero|nethack)|(?:(?:^|[^\w\s])[\s_]*|(?<![a-z])gnu[\s_]+)library)"
    r"[\s_]+$",
    re.IGNORECASE,
)


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


def _is_text(raw: str) -> bool:
    """A value spanning several lines is a license text (a free-text field), not an expression."""
    return len(raw.strip().splitlines()) > 1


def _parse_part(part: str) -> list[list[str]]:
    """One `;`-part: a name found whole in the table is that id (a classifier such as `GNU General
    Public License v3 (GPLv3)`); other parentheses → UNKNOWN; the rest splits on OR/AND/WITH."""
    text = part.strip()
    if text.lower() in NORMALISATION:
        return [[NORMALISATION[text.lower()]]]
    if "(" in text or ")" in text:
        return [[UNKNOWN]]
    return [
        [normalise_id(re.split(r"\s+WITH\s+", term)[0]) for term in re.split(r"\s+AND\s+", alt)]
        for alt in re.split(r"\s+OR\s+", text)
    ]


def parse_expression(raw: str) -> list[list[str]]:
    """OR of ANDs: `GPL-2.0 OR MIT` → [[GPL-2.0], [MIT]]; `MIT AND PSF-2.0` → [[MIT, PSF-2.0]].

    `;` is OR and is split first: pip-licenses `--from=mixed` joins a package's license
    classifiers with `"; "`, so each part is looked up whole before the parenthesis rule applies
    to it (#94). `WITH` keeps the left-hand id. A license text (several lines) is UNKNOWN.
    """
    if _is_text(raw):
        return [[UNKNOWN]]
    return [conj for part in raw.split(";") for conj in _parse_part(part)]


def denied_family(spdx: str, deny: tuple[str, ...]) -> str | None:
    """The deny family of an id: `GPL`, `GPL-3.0-only`, `GPL-2.0+` and the common `GPLv3`,
    `GPL3`, `GPLVersion3` and `GPL.v3` spellings are GPL, after a leading `GNU` (`GNU-GPL`) or
    PEP 639 `LicenseRef-` (`LicenseRef-GPL-3.0`) too; `LGPL-2.1-only` and `AGPL-3.0` are not (each
    family on its own). Only a leading prefix is dropped: the SPDX id `CNRI-Python-GPL-Compatible`
    names a permissive license."""
    upper = re.sub(r"^(?:LICENSEREF-)?(?:GNU[-.+]?)?", "", spdx.upper())
    for fam in sorted(deny, key=len, reverse=True):  # LGPL before GPL
        if re.match(rf"{re.escape(fam)}(?:$|[-+.]|(?:V|VERSION)?\d)", upper):
            return fam
    return None


def _words(text: str) -> str:
    """`Server-Side Public_License` → ` server side public license `: lower-case words, each
    between single spaces, for matching `FAMILY_NAMES` across any separators."""
    return f" {' '.join(re.findall(r'[a-z0-9]+', text.lower()))} "


def named_family(text: str, deny: tuple[str, ...]) -> str | None:
    """A deny family named in free `text` or an id, by identifier (`GNU GPL v3`, `GNU-GPL`,
    `(GPL-3.0-only)`, `LicenseRef-GPLv3`) or by long name (`GNU Public License`,
    `GNU-General-Public-License`, `Affero GPL`). A qualified GPL (`Lesser GPL`) is not the GPL."""
    for token in TOKEN_RE.finditer(text):
        family = denied_family(token[0].strip(".+-"), deny)
        if family == "GPL" and GPL_QUALIFIER_RE.search(text[: token.start()]):
            continue  # the LGPL, AGPL or NGPL, which FAMILY_NAMES reads
        if family:
            return family
    words = _words(text)
    for family, name in FAMILY_NAMES:
        if family in deny and name.search(words):
            return family
    return None


def unread_family(raw: str, deny: tuple[str, ...]) -> str | None:
    """A deny family named where `parse_expression` reads UNKNOWN, so no exception can admit it.

    Searched: each `;`-part that is not in the table and does not parse (free text, an SPDX
    expression with parentheses, an unlisted classifier), and a license text's title line. A
    table entry is authoritative for its spelling. A text's body is not searched: permissive
    texts name the GPL in passing (MPL-2.0's "Secondary License"), and numpy's and scipy's
    `License` fields carry the GPL notice of the runtime library bundled in their wheels.
    """
    if _is_text(raw):
        return named_family(raw.strip().splitlines()[0], deny)
    for part in raw.split(";"):
        if part.strip().lower() in NORMALISATION:
            continue
        if any(UNKNOWN in conj for conj in _parse_part(part)) and (
            family := named_family(part, deny)
        ):
            return family
    return None


@dataclass(frozen=True)
class Verdict:
    package: str
    license: str
    status: str  # ok | denied | not allowed | unknown | excepted
    detail: str = ""


def judge(package: str, raw: str, config: LicensesConfig) -> Verdict:
    alternatives = parse_expression(raw)
    # OR holds between what the gate reads, ids included (`GNU-GPL OR MIT` is `GPL-2.0 OR MIT`).
    # Text it cannot read may bind the package (`MIT; GPL (for the bundled parser)` is prose), so
    # a deny family named there is not rescued by an allowed branch, and no exception admits it
    # (#94).
    unread = unread_family(raw, config.deny)
    if not unread:
        for conj in alternatives:
            if all(x.lower() in config.allow for x in conj):
                return Verdict(package, raw, "ok")
    denied: str | None = None
    for conj in alternatives:
        for x in conj:
            # An id names a family by identifier (`GNU-GPL`) or as a hyphenated long name.
            denied = denied or named_family(x, config.deny)
    denied = denied or unread
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
