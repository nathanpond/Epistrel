from __future__ import annotations

from pathlib import Path

import pytest
from tools.gates.licenses import (
    NORMALISATION,
    UNKNOWN,
    LicensesConfig,
    LicensesError,
    evaluate,
    judge,
    load_licenses_config,
    normalise_id,
    parse_expression,
)

from tests.conftest import REPO_ROOT

REPO_CONFIG = load_licenses_config(REPO_ROOT / "pyproject.toml")
# The repo's allow and deny lists without its exceptions: the synthetic closures below never
# contain the excepted packages, which would otherwise be reported as stale.
CONFIG = LicensesConfig(REPO_CONFIG.allow, REPO_CONFIG.deny, {})
PARENTS = {"fastapi": {"epistrel"}, "starlette": {"fastapi"}, "psycopg": {"epistrel"}}


def _records(**licenses: str) -> list[dict[str, str]]:
    return [{"Name": n, "Version": "1", "License": lic} for n, lic in licenses.items()]


def test_all_allowed_passes() -> None:
    report = evaluate(
        _records(fastapi="MIT", starlette="BSD License", psycopg="Apache-2.0"), PARENTS, CONFIG
    )
    assert report.findings == []
    assert report.judged == 3


def test_gpl_is_denied_and_names_the_direct_dependency() -> None:
    report = evaluate(
        _records(fastapi="MIT", starlette="GPL-3.0-only", psycopg="MIT"), PARENTS, CONFIG
    )
    assert report.findings == ["starlette: 'GPL-3.0-only' denied (GPL) [invariant 2] via fastapi"]


def test_lgpl_is_allowed_for_libraries_but_agpl_names_its_own_family() -> None:
    ok = evaluate(
        _records(fastapi="MIT", starlette="MIT", psycopg="LGPL-3.0-only"), PARENTS, CONFIG
    )
    assert ok.findings == []
    report = evaluate(
        _records(fastapi="MIT", starlette="MIT", psycopg="AGPL-3.0-only"), PARENTS, CONFIG
    )
    assert report.findings == ["psycopg: 'AGPL-3.0-only' denied (AGPL) [invariant 2] via (direct)"]


def test_unknown_and_empty_fail() -> None:
    report = evaluate(_records(fastapi="UNKNOWN", starlette="", psycopg="MIT"), PARENTS, CONFIG)
    assert [f.split(" [")[0] for f in report.findings] == [
        "fastapi: 'UNKNOWN' UNKNOWN license",
        "starlette: '' UNKNOWN license",
    ]


def test_exception_with_rationale_admits_not_allowed_but_never_denied() -> None:
    cfg = LicensesConfig(CONFIG.allow, CONFIG.deny, {"starlette": "vendor says fine"})
    ok = evaluate(_records(fastapi="MIT", starlette="Artistic-2.0", psycopg="MIT"), PARENTS, cfg)
    assert ok.findings == []
    assert ok.excepted == 1
    refused = evaluate(
        _records(fastapi="MIT", starlette="GPL-2.0-only", psycopg="MIT"), PARENTS, cfg
    )
    assert refused.findings == [
        "starlette: 'GPL-2.0-only' denied (exception refused: GPL family) [invariant 2] via fastapi"
    ]


def test_exception_without_rationale_fails_the_check_itself(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text(
        '[tool.epistrel.gates.licenses]\nallow = ["MIT"]\ndeny = ["GPL"]\n'
        'exceptions = { foo = "  " }\n'
    )
    with pytest.raises(LicensesError, match="no rationale"):
        load_licenses_config(py)
    py.write_text(
        '[tool.epistrel.gates.licenses]\nallow = ["MIT", "GPL-2.0-only"]\ndeny = ["GPL"]\n'
    )
    with pytest.raises(LicensesError, match="both allowed and denied"):
        load_licenses_config(py)


def test_stale_exception_is_a_finding_and_redundant_one_a_warning() -> None:
    cfg = LicensesConfig(CONFIG.allow, CONFIG.deny, {"ghost": "x", "fastapi": "y"})
    report = evaluate(_records(fastapi="MIT", starlette="MIT", psycopg="MIT"), PARENTS, cfg)
    assert report.findings == ["stale exception: ghost is not in the runtime closure"]
    assert report.warnings == [
        "warning: exception for fastapi is redundant (license already allowed)"
    ]


def test_closure_package_missing_from_env_warns_and_extras_are_ignored() -> None:
    report = evaluate(_records(fastapi="MIT", psycopg="MIT", pytest="MIT"), PARENTS, CONFIG)
    assert report.findings == []
    assert report.warnings == [
        "warning: starlette is in the runtime closure but not installed here (marker-gated?)"
    ]
    assert report.ignored == 1


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        ("GPL-2.0 OR MIT", "ok"),
        ("MIT AND GPL-2.0-only", "denied"),
        ("LGPL-3.0-only", "ok"),
        ("MIT AND PSF-2.0", "ok"),
        ("Apache Software License; MIT License", "ok"),
        ("GPL-2.0-or-later WITH Classpath-exception-2.0", "denied"),
        ("(MIT OR Apache-2.0)", "unknown"),
        ("Artistic-2.0", "not allowed"),
    ],
)
def test_spdx_expressions(raw: str, status: str) -> None:
    assert judge("pkg", raw, CONFIG).status == status


def test_classifier_spellings_normalise() -> None:
    assert normalise_id("Apache Software License") == "Apache-2.0"
    assert normalise_id("3-Clause BSD License") == "BSD-3-Clause"
    assert normalise_id("Python Software Foundation License") == "PSF-2.0"
    assert normalise_id("GNU Lesser General Public License v3 (LGPLv3)") == "LGPL-3.0-only"
    assert normalise_id("totally made up words") == "UNKNOWN"
    assert parse_expression("A; B") == [["A"], ["B"]]


def test_parenthesised_classifier_is_looked_up_whole() -> None:
    """certifi's metadata reads `Mozilla Public License 2.0 (MPL 2.0)`: a known whole value is
    its id (#74). Complement: other parentheses are still UNKNOWN."""
    assert parse_expression("Mozilla Public License 2.0 (MPL 2.0)") == [["MPL-2.0"]]
    assert judge("certifi", "Mozilla Public License 2.0 (MPL 2.0)", CONFIG).status == "ok"
    assert parse_expression("(MIT OR GPL-3.0)") == [["UNKNOWN"]]


@pytest.mark.parametrize(("spelling", "spdx"), sorted(NORMALISATION.items()))
def test_every_normalisation_entry_round_trips(spelling: str, spdx: str) -> None:
    assert normalise_id(spelling) == spdx
    assert normalise_id(spelling.upper()) == spdx
    # The whole value, alone or `;`-joined as pip-licenses joins classifiers (#94).
    assert parse_expression(spelling.upper()) == [[spdx]]
    assert parse_expression(f"MIT License; {spelling}") == [["MIT"], [spdx]]


def _issue_94_report(license_b: str) -> tuple[int, list[str]]:
    """#94's reproduction: package `b` (under `a`) has an exception and the license given."""
    cfg = LicensesConfig(CONFIG.allow, CONFIG.deny, {"b": "x"})
    records = [{"Name": "a", "License": "MIT"}, {"Name": "b", "License": license_b}]
    report = evaluate(records, {"a": {"epistrel"}, "b": {"a"}}, cfg)
    return report.excepted, report.findings


def test_issue_94_reproduction_refuses_the_exception() -> None:
    """The GPLv3 classifier is denied and its exception refused, alone (#74 fixed that) and
    `;`-joined with another classifier, as pip-licenses `--from=mixed` reports a package with
    several license classifiers (#94)."""
    assert _issue_94_report("GNU General Public License v3 (GPLv3)") == (
        0,
        [
            "b: 'GNU General Public License v3 (GPLv3)' denied (exception refused: GPL family)"
            " [invariant 2] via a"
        ],
    )
    excepted, findings = _issue_94_report(
        "GNU General Public License v3 (GPLv3); GNU Affero General Public License v3"
    )
    assert excepted == 0
    (finding,) = findings
    assert finding.startswith("b: 'GNU General Public License v3 (GPLv3); GNU Affero")
    assert finding.endswith("denied (exception refused: GPL family) [invariant 2] via a")


@pytest.mark.parametrize(
    ("raw", "status", "detail"),
    [
        ("GNU General Public License v3 (GPLv3)", "denied", "GPL"),
        ("ISC License (ISCL)", "ok", ""),
        ("Mozilla Public License 2.0 (MPL 2.0)", "ok", ""),
        ("Zero-Clause BSD (0BSD)", "ok", ""),
        ("The Unlicense (Unlicense)", "ok", ""),
        ("ISC License (ISCL); MIT License", "ok", ""),
        ("Zero-Clause BSD (0BSD); The Unlicense (Unlicense)", "ok", ""),
        (
            "GNU General Public License v3 (GPLv3); GNU Affero General Public License v3",
            "denied",
            "GPL",
        ),
        ("GNU Affero General Public License v3 or later (AGPLv3+); Freeware", "denied", "AGPL"),
        ("European Union Public Licence 1.0 (EUPL 1.0)", "denied", "EUPL"),
        ("GNU Lesser General Public License v3 (LGPLv3); MIT License", "ok", ""),
        ("GNU Library or Lesser General Public License (LGPL)", "ok", ""),
        # `;` is OR (#42): an allowed branch passes, as `GPL-2.0 OR MIT` does (user decision).
        ("GNU General Public License v3 (GPLv3); MIT License", "ok", ""),
    ],
)
def test_classifier_values_are_read_per_classifier(raw: str, status: str, detail: str) -> None:
    verdict = judge("pkg", raw, CONFIG)
    assert (verdict.status, verdict.detail) == (status, detail)


@pytest.mark.parametrize(
    ("raw", "family"),
    [
        ("GNU General Public License v3 (GPLv3)", "GPL"),
        ("gnu general public license v3 (gplv3)", "GPL"),
        ("  GNU GENERAL PUBLIC LICENSE V3 (GPLV3)  ", "GPL"),
        ("GNU General Public License v3 (GPLv3); GNU Affero General Public License v3", "GPL"),
        ("Other/Proprietary License; GNU General Public License v2 or later (GPLv2+)", "GPL"),
        ("European Union Public Licence 1.0 (EUPL 1.0)", "EUPL"),
        ("GPLv3", "GPL"),
        ("GPL3+", "GPL"),
        ("AGPLv3+", "AGPL"),
        ("GNU GPL v3", "GPL"),
        ("GPL (>= 2)", "GPL"),
        ("GNU General Public License v3 or later", "GPL"),
        ("GNU Affero General Public License, version 3", "AGPL"),
        ("European Union Public Licence v. 1.2", "EUPL"),
        ("Server Side Public License, v 1", "SSPL"),
        ("GPL-3.0-only AND (MIT OR Apache-2.0)", "GPL"),
        ("MIT AND GNU GPL v3", "GPL"),
        ("MIT; GPL (for the bundled parser)", "GPL"),
        ("GNU GENERAL PUBLIC LICENSE\n   Version 3, 29 June 2007\n\n Everyone is permitted", "GPL"),
        # The GPL misnomer and the bare long name (#94 verification, round 1).
        ("GNU Public License", "GPL"),
        ("GNU Public License v3", "GPL"),
        ("GNU Public License v2 or later", "GPL"),
        ("General Public License v3", "GPL"),
        ("MIT License; General Public License v3", "GPL"),
        ("GNU Affero Public License v3", "AGPL"),
        # Long names joined by `-` or `_`, and identifiers the parser reads as SPDX-like ids.
        ("Server-Side Public License", "SSPL"),
        ("European_Union_Public_Licence_1.2", "EUPL"),
        ("GNU-General-Public-License-v3", "GPL"),
        ("GNU-GPL", "GPL"),
        ("GNU-GPLv3", "GPL"),
        ("GNU.AGPL-3.0", "AGPL"),
        ("GPL.v3", "GPL"),
        ("MIT AND GPL.v3", "GPL"),
        # PEP 639 `LicenseRef-` ids: the one legal non-SPDX way to name a family in the
        # `License-Expression` field, which pip-licenses reads first (#94 verification, round 2).
        ("LicenseRef-GPL-3.0", "GPL"),
        ("LicenseRef-GPLv3", "GPL"),
        ("licenseref-gnu-gpl-2.0-or-later", "GPL"),
        ("LicenseRef-AGPL-3.0", "AGPL"),
        ("MIT AND LicenseRef-GPL-3.0", "GPL"),
        # `Affero` names only the AGPL; `EU` is the EUPL's short form; a long name's words and
        # version may be joined (CamelCase), and so may an id's `Version`.
        ("Affero GPL", "AGPL"),
        ("Affero-GPL", "AGPL"),
        ("Affero-GPLv3", "AGPL"),
        ("GNU-Affero-GPL", "AGPL"),
        ("GNU Affero v3", "AGPL"),
        ("LicenseRef-Affero-GPL", "AGPL"),
        ("EU Public Licence", "EUPL"),
        ("EuropeanUnionPublicLicence", "EUPL"),
        ("ServerSide Public License", "SSPL"),
        ("GNUGeneralPublicLicense", "GPL"),
        ("GeneralPublicLicenseV3", "GPL"),
        ("EUPublicLicence1.2", "EUPL"),
        ("GPLVersion3", "GPL"),
        # A qualifier (`Lesser`, `Library`, `Affero`, `Nethack`) reaches only the GPL joined to it,
        # and `library` only as the license's own name, not as the kind of package.
        ("Lesser GPL v3 or GNU GPL v2", "GPL"),
        ("Library: GPL v3", "GPL"),
        ("Python library GPL v3", "GPL"),
        ("Library AGPL v3", "AGPL"),
    ],
)
def test_an_exception_never_admits_a_denied_family_whatever_the_spelling(
    raw: str, family: str
) -> None:
    """A deny family is found whether it is a table spelling (alone, `;`-joined, any case), an
    SPDX-like id (`GNU-GPL`, `GPL.v3`, `LicenseRef-GPL-3.0`, a long name joined by hyphens), or
    named where the parser reads UNKNOWN: free text (`GNU Public License`, `Affero GPL`,
    `ServerSide Public License`), an SPDX expression with parentheses, the title line of a
    license text (#94)."""
    verdict = judge("pkg", raw, CONFIG)
    assert (verdict.status, verdict.detail) == ("denied", family)
    cfg = LicensesConfig(CONFIG.allow, CONFIG.deny, {"pkg": "vendor says fine"})
    verdict = judge("pkg", raw, cfg)
    assert (verdict.status, verdict.detail) == ("denied", f"exception refused: {family} family")


MPL_TEXT = (
    "Mozilla Public License Version 2.0\n"
    "==================================\n"
    '1.12. "Secondary License"\n'
    "    means either the GNU General Public License, Version 2.0, the GNU Lesser\n"
    "    General Public License, Version 2.1, the GNU Affero General Public\n"
    "    License, Version 3.0, or any later versions of those licenses."
)
# Abridged from numpy 2.3.1's `License` metadata field (scipy 1.16.1's has the same shape): a
# BSD text that carries the notices of the binaries bundled in the wheel.
NUMPY_TEXT = (
    "Copyright (c) 2005-2025, NumPy Developers.\n"
    "All rights reserved.\n\n"
    "Redistribution and use in source and binary forms, with or without\n"
    "modification, are permitted provided that the following conditions are\n"
    "met:\n\n"
    "This binary distribution of NumPy also bundles the following software:\n\n"
    "Name: GCC runtime library\n"
    "License: GPL-3.0-or-later WITH GCC-exception-3.1\n"
    "  Libgfortran is free software; you can redistribute it and/or modify\n"
    "  it under the terms of the GNU General Public License as published by\n"
    "  the Free Software Foundation; either version 3, or (at your option)\n"
    "  any later version."
)


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        ("Artistic-2.0", "not allowed"),
        ("Other/Proprietary License", "unknown"),
        ("(MIT OR Apache-2.0)", "unknown"),
        ("GNU Lesser General Public License v2 (LGPLv2)", "not allowed"),
        ("LGPLv2+", "not allowed"),
        ("GNU Lesser General Public License v2.1", "unknown"),
        ("GNU Library General Public License v2", "unknown"),
        ("Lesser General Public License v3", "unknown"),
        ("GNU Library Public License", "unknown"),
        ("GNU Free Documentation License (FDL)", "unknown"),
        ("Nethack General Public License", "not allowed"),
        ("CNRI-Python-GPL-Compatible", "not allowed"),
        ("gnuplot", "not allowed"),
        (MPL_TEXT, "unknown"),
        (NUMPY_TEXT, "unknown"),
        # The LGPL's and the Nethack GPL's short names (#94 verification, round 2).
        ("Lesser GPL", "unknown"),
        ("GNU Lesser GPL v3", "unknown"),
        ("Library GPL", "unknown"),
        ("The GNU Library GPL v2", "unknown"),
        ("LGPL (Library GPL)", "unknown"),
        ("GNU_Lesser_GPL_v3", "unknown"),
        ("Nethack GPL", "unknown"),
        ("NetHack General Public License (NGPL)", "unknown"),
        # Only a leading `LicenseRef-` is dropped, as `GNU` is.
        ("LicenseRef-LGPL-2.1", "not allowed"),
        ("LicenseRef-CNRI-Python-GPL-Compatible", "not allowed"),
        # `licensed` is not `license`.
        ("Free for the general public, licensed as MIT", "unknown"),
    ],
)
def test_an_exception_still_admits_what_names_no_denied_family(raw: str, status: str) -> None:
    """Complement: LGPL (by any of its names, `Lesser GPL` and `Library GPL` included), the
    Nethack GPL and the GFDL are not the GPL family. Only a leading `GNU` or `LicenseRef-` is
    dropped from an id, so the SPDX id `CNRI-Python-GPL-Compatible` and `gnuplot` are not GPL.
    Only the title of a license text counts: MPL-2.0's text names the GPL in passing, and numpy's
    and scipy's carry the GPL notice of a bundled runtime library."""
    assert judge("pkg", raw, CONFIG).status == status
    cfg = LicensesConfig(CONFIG.allow, CONFIG.deny, {"pkg": "vendor says fine"})
    assert judge("pkg", raw, cfg).status == "excepted"


def test_a_table_spelling_is_read_as_listed_not_searched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only what the parser cannot read is searched for a family name, so a free-text spelling
    that names the GPL without being GPL is settled by a table entry."""
    assert judge("pkg", "Dual MIT/GPL", CONFIG).detail == "GPL"
    monkeypatch.setitem(NORMALISATION, "dual mit/gpl", "MIT")
    assert judge("pkg", "Dual MIT/GPL", CONFIG).status == "ok"


def test_a_table_entry_of_unknown_leaves_a_false_positive_to_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented remedy for a false positive: a table entry is authoritative even when it
    maps the spelling to UNKNOWN, so the spelling is not searched and an exception with its
    rationale can admit it (#94 verification, round 2)."""
    raw = "MIT-style, GPL compatible"
    cfg = LicensesConfig(CONFIG.allow, CONFIG.deny, {"pkg": "vendor says fine"})
    assert judge("pkg", raw, cfg).detail == "exception refused: GPL family"
    monkeypatch.setitem(NORMALISATION, raw.lower(), UNKNOWN)
    assert judge("pkg", raw, CONFIG).status == "unknown"
    assert judge("pkg", raw, cfg).status == "excepted"


def test_or_rescues_only_what_the_gate_reads() -> None:
    """`;` is OR between parts the gate reads (`GPLv3 classifier; MIT License` passes, as `GPL-2.0
    OR MIT` does), but prose it cannot read may bind the package, so a deny family named there
    denies the value. An SPDX-like id is read even when only its spelling names the family
    (`GNU-GPL OR MIT` is `GPL-2.0 OR MIT`). Complement: unread prose that names no family leaves
    the OR alone, and the LGPL's short name is no family."""
    verdict = judge("pkg", "MIT; GPL (for the bundled parser)", CONFIG)
    assert (verdict.status, verdict.detail) == ("denied", "GPL")
    verdict = judge("pkg", "MIT; GNU Public License", CONFIG)
    assert (verdict.status, verdict.detail) == ("denied", "GPL")
    assert judge("pkg", "GNU General Public License v3 (GPLv3); MIT License", CONFIG).status == "ok"
    assert judge("pkg", "GNU-GPL OR MIT", CONFIG).status == "ok"
    assert judge("pkg", "LicenseRef-GPL-3.0 OR MIT", CONFIG).status == "ok"
    assert judge("pkg", "BSD License; see LICENSE (in the sdist)", CONFIG).status == "ok"
    assert judge("pkg", "MIT License; GNU Lesser GPL v3", CONFIG).status == "ok"


@pytest.mark.parametrize(
    "raw",
    ["GNU Lesser General Public License v2.1", "Lesser GPL", "GNU Lesser GPL v3", "Library GPL"],
)
def test_family_names_follow_the_configured_deny_list(raw: str) -> None:
    """A long name, or a GPL qualified as another license (`Lesser GPL`), counts only for a family
    in `deny`: LGPL is allowed here (invariant 2), but a deny list naming it refuses its exception
    like any other family's."""
    cfg = LicensesConfig(frozenset({"mit"}), ("GPL", "LGPL"), {"pkg": "vendor says fine"})
    verdict = judge("pkg", raw, cfg)
    assert (verdict.status, verdict.detail) == ("denied", "exception refused: LGPL family")
    assert judge("pkg", raw, CONFIG).status == "unknown"


# Every license classifier in trove-classifiers 2026.9.21.13, as pip-licenses reports it (the
# last segment; it drops the bare `OSI Approved`).
TROVE_LICENSES = (
    "Academic Free License (AFL)",
    "Aladdin Free Public License (AFPL)",
    "Apache Software License",
    "Apple Public Source License",
    "Artistic License",
    "Attribution Assurance License",
    "Blue Oak Model License (BlueOak-1.0.0)",
    "Boost Software License 1.0 (BSL-1.0)",
    "BSD License",
    "CC0 1.0 Universal (CC0 1.0) Public Domain Dedication",
    "CEA CNRS Inria Logiciel Libre License, version 2.1 (CeCILL-2.1)",
    "CeCILL-B Free Software License Agreement (CECILL-B)",
    "CeCILL-C Free Software License Agreement (CECILL-C)",
    "CMU License (MIT-CMU)",
    "Common Development and Distribution License 1.0 (CDDL-1.0)",
    "Common Public License",
    "DFSG approved",
    "Eclipse Public License 1.0 (EPL-1.0)",
    "Eclipse Public License 2.0 (EPL-2.0)",
    "Educational Community License, Version 2.0 (ECL-2.0)",
    "Eiffel Forum License",
    "Eiffel Forum License (EFL)",
    "European Union Public Licence 1.0 (EUPL 1.0)",
    "European Union Public Licence 1.1 (EUPL 1.1)",
    "European Union Public Licence 1.2 (EUPL 1.2)",
    "Free For Educational Use",
    "Free For Home Use",
    "Free for non-commercial use",
    "Free To Use But Restricted",
    "Freely Distributable",
    "Freeware",
    "GNU Affero General Public License v3",
    "GNU Affero General Public License v3 or later (AGPLv3+)",
    "GNU Free Documentation License (FDL)",
    "GNU General Public License (GPL)",
    "GNU General Public License v2 (GPLv2)",
    "GNU General Public License v2 or later (GPLv2+)",
    "GNU General Public License v3 (GPLv3)",
    "GNU General Public License v3 or later (GPLv3+)",
    "GNU Lesser General Public License v2 (LGPLv2)",
    "GNU Lesser General Public License v2 or later (LGPLv2+)",
    "GNU Lesser General Public License v3 (LGPLv3)",
    "GNU Lesser General Public License v3 or later (LGPLv3+)",
    "GNU Library or Lesser General Public License (LGPL)",
    "GUST Font License 1.0",
    "GUST Font License 2006-09-30",
    "Historical Permission Notice and Disclaimer (HPND)",
    "IBM Public License",
    "Intel Open Source License",
    "ISC License (ISCL)",
    "Jabber Open Source License",
    "MirOS License (MirOS)",
    "MIT License",
    "MIT No Attribution License (MIT-0)",
    "MITRE Collaborative Virtual Workspace License (CVW)",
    "Motosoto License",
    "Mozilla Public License 1.0 (MPL)",
    "Mozilla Public License 1.1 (MPL 1.1)",
    "Mozilla Public License 2.0 (MPL 2.0)",
    "Mulan Permissive Software License v2 (MulanPSL-2.0)",
    "NASA Open Source Agreement v1.3 (NASA-1.3)",
    "Nethack General Public License",
    "Netscape Public License (NPL)",
    "Nokia Open Source License",
    "Nokia Open Source License (NOKOS)",
    "Open Group Test Suite License",
    "Open Software License 3.0 (OSL-3.0)",
    "Other/Proprietary License",
    "PostgreSQL License",
    "Public Domain",
    "Python License (CNRI Python License)",
    "Python Software Foundation License",
    "Qt Public License (QPL)",
    "Repoze Public License",
    "Ricoh Source Code Public License",
    "SIL Open Font License 1.1 (OFL-1.1)",
    "Sleepycat License",
    "Sun Industry Standards Source License (SISSL)",
    "Sun Public License",
    "The Unlicense (Unlicense)",
    "Universal Permissive License (UPL)",
    "University of Illinois/NCSA Open Source License",
    "Vovida Software License 1.0",
    "W3C License",
    "X.Net License",
    "Zero-Clause BSD (0BSD)",
    "zlib/libpng License",
    "Zope Public License",
)
DENIED_TROVE = {
    "European Union Public Licence 1.0 (EUPL 1.0)": "EUPL",
    "European Union Public Licence 1.1 (EUPL 1.1)": "EUPL",
    "European Union Public Licence 1.2 (EUPL 1.2)": "EUPL",
    "GNU Affero General Public License v3": "AGPL",
    "GNU Affero General Public License v3 or later (AGPLv3+)": "AGPL",
    "GNU General Public License (GPL)": "GPL",
    "GNU General Public License v2 (GPLv2)": "GPL",
    "GNU General Public License v2 or later (GPLv2+)": "GPL",
    "GNU General Public License v3 (GPLv3)": "GPL",
    "GNU General Public License v3 or later (GPLv3+)": "GPL",
}


@pytest.mark.parametrize("classifier", TROVE_LICENSES)
def test_every_trove_license_classifier_is_in_the_table(classifier: str) -> None:
    """#42 promised the full trove set; a missing deny-family classifier (EUPL 1.0) read UNKNOWN,
    which an exception could admit (#94). Complement: no other classifier is denied."""
    assert classifier.lower() in NORMALISATION, classifier
    spdx = NORMALISATION[classifier.lower()]
    assert parse_expression(f"{classifier}; Freeware") == [[spdx], [UNKNOWN]]
    verdict = judge("pkg", classifier, CONFIG)
    if classifier in DENIED_TROVE:
        assert (verdict.status, verdict.detail) == ("denied", DENIED_TROVE[classifier])
    else:
        assert verdict.status != "denied", verdict
