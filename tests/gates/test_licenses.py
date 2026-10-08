from __future__ import annotations

from pathlib import Path

import pytest
from tools.gates.licenses import (
    NORMALISATION,
    LicensesConfig,
    LicensesError,
    evaluate,
    judge,
    load_licenses_config,
    normalise_id,
    parse_expression,
)

from tests.conftest import REPO_ROOT

CONFIG = load_licenses_config(REPO_ROOT / "pyproject.toml")
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


def test_lgpl_names_its_own_family_not_gpl() -> None:
    report = evaluate(
        _records(fastapi="MIT", starlette="MIT", psycopg="LGPL-3.0-only"), PARENTS, CONFIG
    )
    assert report.findings == ["psycopg: 'LGPL-3.0-only' denied (LGPL) [invariant 2] via (direct)"]


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
        _records(fastapi="MIT", starlette="LGPL-2.1-or-later", psycopg="MIT"), PARENTS, cfg
    )
    assert refused.findings == [
        "starlette: 'LGPL-2.1-or-later' denied (exception refused: LGPL family) "
        "[invariant 2] via fastapi"
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


@pytest.mark.parametrize(("spelling", "spdx"), sorted(NORMALISATION.items()))
def test_every_normalisation_entry_round_trips(spelling: str, spdx: str) -> None:
    assert normalise_id(spelling) == spdx
    assert normalise_id(spelling.upper()) == spdx
