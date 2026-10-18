"""Ownership's rules without a database (#104): only the ownership code reads relations and no
code interprets a relation's name; the trust-boundary statement and the OpenAPI document say
that ownership confers nothing; names and filters parse as documented; the registry covers
every owned type."""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from pathlib import Path

import pytest
from pydantic import ValidationError

import epistrel
from epistrel.api.auth import TRUST_BOUNDARY_STATEMENT
from epistrel.api.routes.ownership import CONFERS_NOTHING, OWNERSHIP_TAG
from epistrel.core.context import PRINCIPAL_PATTERN, has_control, principal_problem
from epistrel.core.erasure.boundary import render_boundary
from epistrel.core.errors import InvalidRequest
from epistrel.core.ownership import (
    REGISTRY,
    RELATION_FILTER_PATTERN,
    SEGMENTS,
    RelationFilter,
    RelationRequest,
    owner_filter,
    parse_relation_filter,
    path_principal,
    path_relation,
    relation_name_problem,
)
from epistrel.core.principals import lookup_principal_id
from epistrel.core.settings_schema import LISTING_STATUS_PATTERN
from epistrel.core.tables.ownership import EntityType
from tests.conftest import REPO_ROOT

SRC = Path(epistrel.__file__).resolve().parent
RELATIONS_TABLE = "entity_relations"
READERS = frozenset(
    {
        "core/ownership.py",  # relation writes, the list filters, drop_entity
        "core/tables/ownership.py",  # the table's declaration
        "core/tables/__init__.py",  # registers it with the metadata
        "core/principals.py",  # principal lookup (#117)
        "core/erasure/index.py",  # names it among the tables the subject index exempts (#334)
    }
)
"""The only source files that may name ``entity_relations``. #110 adds its catalog list query
module here if it filters without ``core.ownership.list_filters``."""


def _names_relations(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == RELATIONS_TABLE
    if isinstance(node, ast.Attribute):
        return node.attr == RELATIONS_TABLE
    if isinstance(node, ast.alias):
        return RELATIONS_TABLE in node.name.split(".")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return RELATIONS_TABLE in node.value
    return False


def _compares_a_name_to_a_literal(node: ast.AST) -> bool:
    """``….relation == "editor"`` (either side): code that decides on a relation's name."""
    if not isinstance(node, ast.Compare):
        return False
    sides = [node.left, *node.comparators]
    named = any(isinstance(side, ast.Attribute) and side.attr == "relation" for side in sides)
    literal = any(isinstance(side, ast.Constant) and isinstance(side.value, str) for side in sides)
    return named and literal


def relation_readers(root: Path) -> tuple[set[str], set[str]]:
    """``(files that name entity_relations, files that compare a relation to a literal)``."""
    readers: set[str] = set()
    deciders: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        name = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if _names_relations(node):
                readers.add(name)
            if _compares_a_name_to_a_literal(node):
                deciders.add(name)
    return readers, deciders


def test_only_ownership_code_reads_relations(tmp_path: Path) -> None:
    """No code path interprets a relation's name (#104's AC2). An AST scan of ``src/`` finds
    ``entity_relations`` named only by the ownership module (relation writes, the ``owner`` and
    ``relation`` list filters, ``drop_entity``), the table's declaration and principal lookup.
    No source compares a relation to a literal name.

    Complement asserted: the scan finds the ownership module, and it flags a planted module that
    gates on ``relation == "admin"``."""
    readers, deciders = relation_readers(SRC)
    assert "core/ownership.py" in readers
    assert readers <= READERS, readers - READERS
    assert deciders == set()

    planted = tmp_path / "pkg"
    planted.mkdir()
    (planted / "gate.py").write_text(
        "from epistrel.core.tables import entity_relations\n\n"
        "def allowed(row):\n"
        '    return row.relation == "admin"\n',
        encoding="utf-8",
    )
    assert relation_readers(planted) == ({"gate.py"}, {"gate.py"})


def test_documentation_says_ownership_confers_nothing() -> None:
    """The trust-boundary statement says that ownership exists for listing, attribution and
    offboarding only, and that nothing is granted or withheld because of an owner or a relation.
    The statement opens the OpenAPI description (the erasure boundary, #125, follows it) and
    appears in the README's trust-boundary paragraph. The ``ownership`` tag says it, and so does
    every owner and relation route's description."""
    assert TRUST_BOUNDARY_STATEMENT.endswith(
        "Ownership and relations are labels the consumer defines; the engine never grants or "
        "withholds an action because of them. Ownership exists for listing, attribution and "
        "offboarding only."
    )
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    paragraph = readme.split("## Trust boundary", 1)[1].split("\n## ", 1)[0]
    assert TRUST_BOUNDARY_STATEMENT in paragraph
    document = json.loads((REPO_ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    # #125 appends the erasure boundary after a blank line; the statement still leads.
    assert document["info"]["description"] == f"{TRUST_BOUNDARY_STATEMENT}\n\n{render_boundary()}"
    tags = {tag["name"]: tag["description"] for tag in document["tags"]}
    assert "nothing gates on them" in tags[OWNERSHIP_TAG]
    owned = [
        operation
        for item in document["paths"].values()
        for operation in item.values()
        if operation.get("tags") == [OWNERSHIP_TAG]
    ]
    assert len(owned) >= 8  # stories and catalog characters, four routes each
    assert all(CONFERS_NOTHING in operation["description"] for operation in owned)


def test_relation_names_and_filters() -> None:
    """A relation name is 1 to 64 of ``A-Z a-z 0-9 _ -``, trimmed and case-sensitive. Both
    bounds hold wherever a name goes: names of exactly 1 and exactly 64 characters pass the name
    rule, the request body, a path and a filter unchanged, and one character more passes none of
    them. A filter is ``name:principal``, or ``name:`` for any principal, split at the first
    colon (a principal may hold colons and slashes); a bare word is refused. An ``owner`` filter
    may name ``system``; a principal named in a path may not."""
    for good in ("q", "co_author-2", "x" * 64):
        assert relation_name_problem(good) is None, good
        assert RelationRequest(principal="p-1", relation=f" {good}\t").relation == good
        assert path_relation(good) == good
        assert parse_relation_filter(f"{good}:p-1") == RelationFilter(good, "p-1")
    for bad in ("", "co author", "a:b", "a/b", "x" * 65, "é"):
        assert relation_name_problem(bad) is not None, bad
        with pytest.raises(ValidationError):
            RelationRequest(principal="p-1", relation=bad)
    assert path_relation(" editor\t") == "editor"
    assert parse_relation_filter("editor:") == RelationFilter("editor")
    assert parse_relation_filter("editor: \t") == RelationFilter("editor")
    assert parse_relation_filter("editor:urn:team/ana") == RelationFilter("editor", "urn:team/ana")
    assert parse_relation_filter("editor: p-1 ") == RelationFilter("editor", "p-1")
    assert owner_filter(" system ") == "system"
    assert owner_filter(None) is None
    assert path_principal(" p-1\t") == "p-1"
    for call, field in (
        (lambda: parse_relation_filter("bad name:p"), "query.relation"),
        (lambda: parse_relation_filter("editor"), "query.relation"),
        (lambda: parse_relation_filter("true"), "query.relation"),
        (lambda: parse_relation_filter("editor:a\x01b"), "query.relation"),
        (lambda: parse_relation_filter("editor:a\x85b"), "query.relation"),
        (lambda: parse_relation_filter(f"{'x' * 65}:p-1"), "query.relation"),
        (lambda: owner_filter("\t"), "query.owner"),
        (lambda: path_relation("a b"), "path.relation"),
        (lambda: path_relation("x" * 65), "path.relation"),
        (lambda: path_principal("system"), "path.principal"),
    ):
        with pytest.raises(InvalidRequest) as refused:
            call()
        assert refused.value.details["field"] == field


def test_principal_rule_refuses_every_control_character() -> None:
    """A principal holds no control character (#321): every code point Unicode classes ``Cc``
    (C0, DEL and C1, U+0080 to U+009F) is refused, and no other, by ``principal_problem``, the
    published ``PRINCIPAL_PATTERN``, and the principal half of ``RELATION_FILTER_PATTERN`` alike;
    the listing status pattern (#110) shares the one definition. Complement: a principal with any
    other character passes all of them."""
    principal = re.compile(PRINCIPAL_PATTERN)
    relation = re.compile(RELATION_FILTER_PATTERN)
    listing = re.compile(LISTING_STATUS_PATTERN)
    controls = []
    for code in range(0x110000):
        ch = chr(code)
        control = unicodedata.category(ch) == "Cc"
        value = f"a{ch}b"
        assert has_control(value) is control, hex(code)
        assert (principal_problem(value) is not None) is control, hex(code)
        assert (principal.fullmatch(value) is None) is control, hex(code)
        assert (relation.fullmatch(f"editor:{value}") is None) is control, hex(code)
        if control:
            assert listing.fullmatch(value) is None, hex(code)
            controls.append(code)
    assert controls == [*range(0x20), *range(0x7F, 0xA0)]
    for c1 in ("\x80", "\x85", "\x9f"):
        with pytest.raises(ValidationError):
            RelationRequest(principal=f"p{c1}a", relation="editor")
        for check, field in (
            (lambda v: parse_relation_filter(f"editor:{v}"), "query.relation"),
            (owner_filter, "query.owner"),
            (path_principal, "path.principal"),
            (lookup_principal_id, "path.principal_id"),
        ):
            with pytest.raises(InvalidRequest) as refused:
                check(f"p{c1}a")
            assert refused.value.details["field"] == field


def test_registry_names_every_owned_type() -> None:
    """The registry has one entry per owned type, with distinct URL segments and path
    parameters; a companion rule names a type the registry knows, from both sides."""
    assert set(REGISTRY) == set(EntityType)
    assert len(SEGMENTS) == len(REGISTRY)
    assert len({owned.id_param for owned in REGISTRY.values()}) == len(REGISTRY)
    for owned in REGISTRY.values():
        for link in owned.companions:
            follower = REGISTRY[link.entity_type].follows
            assert follower is not None
            assert (follower.entity_type, follower.column) == (owned.entity_type, link.column)
