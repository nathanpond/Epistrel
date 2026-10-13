"""What every check of a catalog validation shares (FR-SCN-5; #111).

A validation runs in two phases (``core.catalog.validation``):

- **prepare**, outside any transaction: a check that may call a model (the input policies)
  does that work here, so no database lock is ever held across a model call (FR-CONC-2). It
  receives a ``Preparation`` (the engine, the deployment's settings, the ports).
- **run**, inside the caller's transaction (a publish holds the entry's row lock): every check
  reads what it needs through ``ValidationContext.conn`` and returns its ``Findings``. A check
  that prepared reads its result from ``ValidationContext.prepared``.

Each check declares the entry types it applies to (``kinds``) and the capability it needs
(``capability``): the runner lists a check whose capability is not in the context's
``capabilities`` as ``unavailable`` instead of running it. The deployment's capabilities are
#106's delivered modules: each delivered module's name, the pipeline by its level
(``DEPLOYMENT_CAPABILITIES``; in M3 ``lite``, ``rolling_summary``, ``pins``, ``polish``,
``budgets`` and ``models``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.catalog.report import Entry, EntryLocation, Outcome, VersionLabel
from epistrel.core.ports import Ports
from epistrel.core.profiles.schema import DELIVERED
from epistrel.core.settings_schema import DeploymentSettings
from epistrel.core.tables.ownership import EntityType

PIPELINE = "pipeline"


def delivered_capabilities() -> frozenset[str]:
    """The capabilities this deployment has delivered (#106's ``DELIVERED``): every module's
    name, except the pipeline, which is named by each of its delivered levels."""
    found: set[str] = set()
    for module, levels in DELIVERED.items():
        if module == PIPELINE:
            found.update(levels)
        else:
            found.add(module)
    return frozenset(found)


DEPLOYMENT_CAPABILITIES = delivered_capabilities()
"""What this deployment can check: in M3 the ``lite`` pipeline and the delivered modules, so a
check that needs ``structured`` or ``states`` is listed ``unavailable``."""


@dataclass(slots=True)
class Subject:
    """What is validated: an entry of ``entity_type`` (``entry_id``), its ``version`` (a
    published version's number, or ``"draft"``), the stored ``document``, and the model the
    document is validated as (the kind's draft model). ``memo`` holds what one check derives
    and another reuses within this one validation (the parsed document, the role characters)."""

    entity_type: EntityType
    entry_id: str
    version: VersionLabel
    document: Mapping[str, Any]
    model: type[BaseModel]
    memo: dict[str, Any] = field(default_factory=dict)

    def entry(
        self,
        code: str,
        outcome: Outcome,
        *,
        field: str | None = None,
        role: str | None = None,
        seed_id: str | None = None,
        text: str | None = None,
        source: str | None = None,
        rule: str | None = None,
    ) -> Entry:
        """An entry located in this subject."""
        return Entry(
            code=code,
            location=EntryLocation(
                entry=self.entry_id,
                version=self.version,
                role=role,
                field=field,
                seed_id=seed_id,
            ),
            text=text,
            outcome=outcome,
            source=source,
            rule=rule,
        )


@dataclass(frozen=True, slots=True)
class Preparation:
    """What a check's prepare phase may use: the engine (no transaction is open), the
    deployment's settings and the ports to the Orchestrator."""

    db: AsyncEngine
    settings: DeploymentSettings
    ports: Ports = field(default_factory=Ports)


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """What a check's run may consult: the caller's connection (inside a publish's transaction,
    the entry locked; a dry run's read-only connection otherwise), the deployment's settings,
    the ports to the Orchestrator (#103), the capabilities checks may run with, and what each
    check prepared, by check name."""

    conn: AsyncConnection
    settings: DeploymentSettings
    ports: Ports = field(default_factory=Ports)
    capabilities: frozenset[str] = DEPLOYMENT_CAPABILITIES
    prepared: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Findings:
    """What one check found: its entries, and the names of what it could not check."""

    entries: list[Entry] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)


class Check(ABC):
    """One check of the runner (see the module docstring). ``name`` is the name the report's
    ``unavailable`` uses for it; ``gate``: its errors stop the checks after it (the document
    check: the others read a valid document)."""

    name: ClassVar[str]
    capability: ClassVar[str | None] = None
    kinds: ClassVar[frozenset[EntityType]] = frozenset({EntityType.SCENARIO})
    gate: ClassVar[bool] = False

    async def prepare(self, subject: Subject, env: Preparation) -> Any:
        """The work done before the transaction (model calls); nothing by default."""
        return None

    @abstractmethod
    async def run(self, subject: Subject, ctx: ValidationContext) -> Findings:
        """The check, inside the caller's transaction."""


__all__ = [
    "DEPLOYMENT_CAPABILITIES",
    "Check",
    "Findings",
    "Preparation",
    "Subject",
    "ValidationContext",
    "delivered_capabilities",
]
