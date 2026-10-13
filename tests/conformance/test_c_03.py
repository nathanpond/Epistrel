"""C-03: library parity (docs/04), M2 part (FR-API-3; #73): every operation, run through the
in-process ``Engine`` and through the REST API with the same inputs on the stub adapter, gives
identical results.

Each surface gets its own fresh database (migrated to head), and the same scripted sequence of
calls (``STEPS``) runs on both, so stories, ratings, policies and deployment settings evolve the
same way and even the revisions must agree. A library result is rendered through the response
model its route uses (``…Body.of``), so the two surfaces are compared on the JSON a consumer
sees. Normalised away: ids (each ULID becomes its first-seen position), configuration
fingerprints (likewise: a fingerprint hashes its profile's id, which each surface mints; #109),
timestamps, durations, the stub's request ids and the echoed ``Idempotency-Key``;
``Prefer: respond-async``, 204 bodies and replay flags are out of scope.

The parity list is generated from ``Engine``'s public operations (``epistrel.engine.operations``):
an operation without a route, or without a step here, fails ``test_c_03``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel, SecretStr

from epistrel.api import create_app
from epistrel.api.config_changes import ConfigChangesBody
from epistrel.api.routes.admin.model_config import (
    ModelConfigBody,
    ModelConfigEntry,
    ModelConfigItem,
    ModelConfigMutation,
)
from epistrel.api.routes.admin.policies import PolicyBody, PolicyList, PolicyMutation
from epistrel.api.routes.admin.ratings import RatingDetailBody, RatingList, RatingMutation
from epistrel.api.routes.admin.settings import SettingsBody
from epistrel.api.routes.admin.stories import RebuildQueuedResponse, RebuildResponse
from epistrel.api.routes.catalog_characters import (
    CharacterDraftBody,
    CharacterListBody,
    CharacterPublishedBody,
    CharacterStatusBody,
    CharacterVersionsBody,
)
from epistrel.api.routes.catalog_listing import ListingStatusBody
from epistrel.api.routes.catalog_scenarios import (
    ScenarioDraftBody,
    ScenarioListBody,
    ScenarioPublishedBody,
    ScenarioStatusBody,
    ScenarioVersionsBody,
)
from epistrel.api.routes.engine_profiles import (
    ProfileBody,
    ProfileDeletedBody,
    ProfileEditBody,
    ProfileListBody,
    ProfileSchemaBody,
    ProfileVersionsBody,
)
from epistrel.api.routes.history import HistoryResponse
from epistrel.api.routes.media import (
    MediaListBody,
    MediaUploadedBody,
    MediaUrlBody,
    PartStoredBody,
    UploadOpenedBody,
)
from epistrel.api.routes.operations import OperationBody
from epistrel.api.routes.ownership import (
    OwnerBody,
    RelationBody,
    RelationsBody,
    StoryOwnerBody,
    StoryRelationBody,
)
from epistrel.api.routes.respond import RespondResponse
from epistrel.api.routes.stories import (
    IngestResponse,
    StatusBody,
    StoryBody,
    StoryCreatedBody,
    StoryList,
    StorySettingsBody,
    StorySettingsWriteBody,
)
from epistrel.api.routes.story_texts import (
    CustomInstructionsBody,
    CustomInstructionsWritten,
    PinnedMemoriesBody,
    PinnedMemoriesWritten,
)
from epistrel.api.routes.timeline import (
    ForkedBody,
    RegeneratedBody,
    SwappedBody,
    TruncatedBody,
    UntruncatedBody,
    VariantsBody,
)
from epistrel.config import Settings
from epistrel.core.catalog.characters import (
    CharacterCreate,
    CharacterPatch,
    Projection,
    PublishRequest,
)
from epistrel.core.catalog.listing import ListingStatusRequest
from epistrel.core.catalog.scenario_models import ScenarioCreate, ScenarioPatch
from epistrel.core.config_changes import ConfigScope
from epistrel.core.context import RequestContext
from epistrel.core.db import make_engine
from epistrel.core.deployment import SettingsPatch
from epistrel.core.errors import EpistrelError
from epistrel.core.fork import ForkRequest
from epistrel.core.ingest import IngestRequest
from epistrel.core.media.assets import MediaUpload
from epistrel.core.media.uploads import UploadOpen
from epistrel.core.ownership import OwnerTransferRequest, RelationRequest
from epistrel.core.policies.model import Scope
from epistrel.core.profiles.impact import ImpactPreviewRequest
from epistrel.core.profiles.store import ProfileCopy, ProfileCreate, ProfileUpdate, ProfileValidate
from epistrel.core.ratings import RatingFields, RatingReplacement
from epistrel.core.seeds.profiles import LITE_PRESET_ID
from epistrel.core.seeds.ratings import EVERYONE_ID
from epistrel.core.stories import StorySpec
from epistrel.core.story_texts import CustomInstructionsRequest, PinnedMemoriesRequest
from epistrel.core.tables.ownership import EntityType
from epistrel.core.timeline import TruncateRequest, UntruncateRequest
from epistrel.core.variants import ChainRef, SwapRequest
from epistrel.engine import Engine, operations
from epistrel.orchestrator.jobs.handlers.rebuild import RebuildQueued
from epistrel.orchestrator.lite.schemas import RegenerateRequest, RespondRequest
from epistrel.orchestrator.models.config import ConfigReplacement
from epistrel.orchestrator.policies.schemas import PolicyFields, PolicyReplacement
from epistrel.orchestrator.policies.seeds.policies import MINORS_POLICY_ID
from epistrel.testing.normalize import Normalizer
from tests.auth_support import auth_settings, bearer
from tests.conformance.conftest import migrated_database
from tests.media_support import files, metadata, png
from tests.model_support import config_key
from tests.scenario_support import complete as complete_scenario
from tests.story_support import story_body

PRINCIPAL = "p-parity"
CTX = RequestContext(principal=PRINCIPAL)
REPLY = "The innkeeper sets down a mug and says nothing."
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
SIGNED = re.compile(r"([?&](?:exp|sig)=)[^&]*")
"""A signed media URL's expiry and signature (#107): the signature covers the real asset id."""
FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
"""A configuration's fingerprint (#109): it hashes the profile's id, minted per surface."""
DROPPED = frozenset({"idempotency_key"})
"""Transport fields: the key REST echoes (generated per request)."""
ZEROED = frozenset(
    {"elapsed_ms", "latency_ms", "queued_ms", "first_call_offset_ms", "request_id", "policy_set"}
)
"""Durations (#76: a turn's queue wait and first-call delay too), and hashes over ids: the
stub's request id (of the prompt) and an operation's ``config_revisions.policy_set`` (of the
policy ids in force)."""

Ids = dict[str, Any]
Rendered = tuple[int, dict[str, Any]]


# -- one call, on either surface ----------------------------------------------------------------


@dataclass(frozen=True)
class Rest:
    """The REST form of a step: method, path (``{name}`` filled from the surface's ids), JSON
    body, query parameters, a multipart body (``files``, an upload's; #105), and a raw body
    (``content``, an upload part's; #107)."""

    method: str
    path: str
    json: Any = None
    params: dict[str, Any] = field(default_factory=dict)
    files: dict[str, Any] | None = None
    content: bytes | None = None


Library = Callable[[Engine, Ids], Awaitable[Any]]
"""The library form of a step: the Engine call, returning the rendered JSON body."""


@dataclass(frozen=True)
class Step:
    operation: str
    rest: Callable[[Ids], Rest]
    library: Library
    capture: Callable[[dict[str, Any], Ids], None] = lambda body, ids: None
    group: str = ""
    """Steps checked together by their statuses (``ENTRY_GROUP``)."""

    @property
    def label(self) -> str:
        return self.operation


def dump(model: BaseModel, *, exclude_unset: bool = False) -> dict[str, Any]:
    dumped: dict[str, Any] = model.model_dump(
        mode="json", by_alias=True, exclude_unset=exclude_unset
    )
    return dumped


def body_of(model: BaseModel) -> Callable[[Any], dict[str, Any]]:
    return lambda _: dump(model)


async def _story_body(engine: Engine, story_id: str, viewpoint: str | None) -> dict[str, Any]:
    story = await engine.get_story(CTX, story_id, viewpoint=viewpoint)
    undo = await engine.undo_state(CTX, story_id)
    chains = await engine.variants(CTX, story_id)
    return dump(StoryBody.of(story, undo, chains))


async def _settings_body(engine: Engine, story_id: str) -> dict[str, Any]:
    settings, guardrails = await engine.story_settings(CTX, story_id)
    budget = await engine.effective_budget(CTX, story_id)
    return dump(StorySettingsBody.of(settings, guardrails, budget))


async def _settings_write(engine: Engine, story_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    write = await engine.update_story_settings(CTX, story_id, patch)
    _, guardrails = await engine.story_settings(CTX, story_id)
    budget = await engine.effective_budget(CTX, story_id)
    return dump(StorySettingsWriteBody.of(write, guardrails, budget))


async def _model_config_write(engine: Engine, purpose: str, write: Any) -> dict[str, Any]:
    if not write.changed:
        return {}
    _, entry = await engine.model_config_entry(CTX, purpose)
    fields = ModelConfigEntry.fields_of(entry)
    return dump(
        ModelConfigMutation(purpose=purpose, config_revision=write.config_revision, **fields)
    )


async def _rebuild(engine: Engine, story_id: str) -> dict[str, Any]:
    result = await engine.rebuild_projections(CTX, story_id)
    if isinstance(result, RebuildQueued):
        return dump(RebuildQueuedResponse.of(result))
    return dump(RebuildResponse.of(result))


def _keep(name: str, *path: str | int) -> Callable[[dict[str, Any], Ids], None]:
    """Capture ``body[path…]`` as ``ids[name]``."""

    def capture(body: dict[str, Any], ids: Ids) -> None:
        value: Any = body
        for part in path:
            value = value[part]
        ids[name] = value

    return capture


def _keep_story(body: dict[str, Any], ids: Ids) -> None:
    ids["story"] = body["story_id"]
    ids["wren"] = body["characters"][0]["id"]
    ids["ada"] = body["characters"][1]["id"]


def _respond_request(ids: Ids) -> dict[str, Any]:
    return {
        "actor_character_id": ids["wren"],
        "player_input": {"say": "Is there a room for the night?"},
        "prompt_params": {"length": "short"},
    }


def _ingest_request() -> dict[str, Any]:
    return {
        "events": [
            {
                "client_event_id": "evt-1",
                "kind": "narration",
                "text": "Rain drums on the shutters.",
            }
        ]
    }


STORY_POLICY = {
    "name": "Never name the river",
    "scope": "story",
    "rule": {"type": "wordlist", "words": ["zzqx river"]},
    "message": "The river stays nameless.",
}
GLOBAL_POLICY = {
    "name": "Parity global",
    "scope": "global",
    "applies_to": ["text_output"],
    "rule": {"type": "wordlist", "words": ["zzqx"]},
    "message": "Never say zzqx.",
}
RATING = {"name": "Parity rating", "description": "Made by C-03.", "inherits_from": [EVERYONE_ID]}
CHARACTER = {
    "name": "Mara Venn",
    "public_description": "Keeps the light on Gull Rock.",
    "definition": {"personality": "Patient, private and slow to trust."},
}
CHARACTER_PATCH = {"draft_revision": 1, "public_description": "Keeps the Gull Rock light."}
GHOST = {"name": "Ghost", "public_description": "Never published."}
SCENARIO = complete_scenario()
"""A scenario filling every group (#108): its seeds' ids are minted per surface, as ids are."""
SCENARIO_PATCH = {"draft_revision": 1, "drivers": {"plot_hooks": ["A stranger knocks."]}}
SCENARIO_GHOST = {"public_card": {"name": "Ghost"}}
ENTRY_ANSWERS = [
    ("create_scenario_draft", 201),
    ("save_scenario_draft", 200),
    ("save_scenario_draft", 409),
    ("publish_scenario", 201),
    ("get_scenario", 200),
    ("get_scenario", 200),
    ("get_scenario", 200),
    ("list_scenario_versions", 200),
    ("archive_scenario", 200),
    ("unarchive_scenario", 200),
    ("create_scenario_draft", 201),
    ("get_scenario", 422),
    ("delete_scenario", 204),
    ("transfer_owner", 200),
    ("add_relation", 201),
    ("list_relations", 200),
]
"""What the scenario steps (#108) answer on both surfaces: a draft created, saved, refused as
stale, published, read as its card, as version 1 in full and as its draft in full, its versions
listed, archived and unarchived; a second draft never published (422 ``not_published``) and
deleted; and the first scenario's owner and relations."""
ENTRY_GROUP = "scenario"
"""The ``Step.group`` of the scenario steps (#108)."""
PUBLIC = {"value": "public"}
CHARACTER_SEARCH = {"q": "Mara", "listing_status": "public"}
SCENARIO_SEARCH = {"label": ["family"]}
LISTING_ANSWERS = [
    ("set_listing_status", 200),
    ("search_catalog", 200),
    ("search_catalog", 200),
]
"""What the listing steps (#110) answer on both surfaces: the character's listing status set,
the character found by a search filtered by that status, and the scenarios listed by a label."""
LISTING_GROUP = "listing"
"""The ``Step.group`` of the listing steps (#110)."""
NEW_OWNER = {"principal": "p-parity-owner"}
RELATION = {"principal": "p-parity-d", "relation": "co_author"}
MODEL_OVERRIDE = {"model": "parity-model", "params": {"temperature": 0.5}}
MEDIA_BYTES = png(nonce="c-03 parity")
MEDIA = metadata(
    alt_text="The lighthouse door",
    caption="Gull Rock",
    generation_metadata={"model": "sdxl", "seed": 9},
    depicted_subjects=[{"kind": "persona", "id": "01J9Z8Q4N5V6W7X8Y9Z0A1B2C9"}],
)
UNDECLARED = metadata(depicted_subjects=None)
MEDIA_ANSWERS = [
    ("upload_media", 201),
    ("upload_media", 422),
    ("get_media", 200),
    ("get_media_content", 200),
    ("list_media", 200),
    ("get_media_url", 200),
    ("delete_media", 204),
    ("get_media", 200),
]
"""What the media steps (#105) answer on both surfaces: an upload, one refused for its missing
``depicted_subjects``, the reads, a signed URL (#107), the delete (a retire) and a read of the
retired asset."""
SESSION_BYTES = png(nonce="c-03 session")
SESSION = {
    **metadata(caption="Sent in parts"),
    "size": len(SESSION_BYTES),
    "mime_type": "image/png",
    "sha256": hashlib.sha256(SESSION_BYTES).hexdigest(),
}
PUBLIC_BASE = {"public_base_url": "https://media.example.test"}
SESSION_ANSWERS = [
    ("open_upload", 201),
    ("put_upload_part", 200),
    ("get_upload", 200),
    ("complete_upload", 201),
    ("open_upload", 201),
    ("abort_upload", 204),
]
"""What the resumable-upload steps (#107) answer on both surfaces: a session opened, its one
part sent, read back and completed into an asset, and a second session opened and aborted."""
SESSION_OPERATIONS = frozenset(operation for operation, _ in SESSION_ANSWERS)
PROFILE = {
    "name": "Parity profile",
    "settings": {
        "memory": {"rolling_summary": {"window_messages": 24}},
        "budgets": {"deadline_ms": 30_000},
    },
}
PROFILE_EDIT = {"name": "Parity profile", "settings": {"pins": {"max_pinned": 10}}}
PROFILE_COPY = {"name": "Parity copy"}
PROFILE_DRY_RUN = {"settings": {"pipeline": "structured"}}
PROFILE_ANSWERS = [
    ("create_profile", 201),
    ("get_profile", 200),
    ("list_profiles", 200),
    ("update_profile", 200),
    ("get_profile", 200),
    ("list_profile_versions", 200),
    ("copy_profile", 201),
    ("validate_profile", 200),
    ("validate_profile", 200),
    ("validate_profile", 422),
    ("profile_schema", 200),
    ("delete_profile", 409),
    ("delete_profile", 200),
    ("transfer_owner", 200),
    ("impact_preview", 200),
    ("get_effective_config", 200),
]
"""What the engine-profile steps (#106) answer on both surfaces: a profile created, read, listed,
edited into version 2 (two-stage, #109: no story follows it, so it applies at once), read at
version 1, its versions listed, copied, validated (and refused for an undelivered level), the
schema read, the default refused deletion, the copy deleted, and the profile transferred; then
(#109) the impact of assigning it to the story previewed, and the story's effective
configuration read."""
PROFILE_GROUP = "profiles"
"""The ``Step.group`` of the engine-profile steps (#106)."""


def _steps() -> list[Step]:
    """The scripted sequence, in order; each step runs on both surfaces."""
    s = "/stories/{story}"
    return [
        # -- story lifecycle and settings
        Step(
            "create_story",
            lambda ids: Rest("POST", "/stories", story_body("Wren", "Ada")),
            lambda e, ids: _created(e),
            _keep_story,
        ),
        Step(
            "list_stories",
            lambda ids: Rest("GET", "/stories", params={"status": "active"}),
            lambda e, ids: _listed(e),
        ),
        Step(
            "get_story",
            lambda ids: Rest("GET", s, params={"viewpoint": "omniscient"}),
            lambda e, ids: _story_body(e, ids["story"], "omniscient"),
        ),
        Step(
            "update_story_settings",
            lambda ids: Rest(
                "PATCH", f"{s}/settings", {"turn_deadline": 40, "policies": [STORY_POLICY]}
            ),
            lambda e, ids: _settings_write(
                e, ids["story"], {"turn_deadline": 40, "policies": [STORY_POLICY]}
            ),
        ),
        Step(
            "story_settings",
            lambda ids: Rest("GET", f"{s}/settings"),
            lambda e, ids: _settings_body(e, ids["story"]),
        ),
        Step(
            "config_changes",
            lambda ids: Rest("GET", f"{s}/config-changes"),
            lambda e, ids: _changes(e, ConfigScope.STORY, ids["story"]),
        ),
        Step(
            "set_pinned_memories",
            lambda ids: Rest(
                "PUT", f"{s}/pinned-memories", {"items": [{"text": "Ada owes Wren."}]}
            ),
            lambda e, ids: _pins(e, ids["story"]),
        ),
        Step(
            "pinned_memories",
            lambda ids: Rest("GET", f"{s}/pinned-memories"),
            lambda e, ids: _read_pins(e, ids["story"]),
        ),
        Step(
            "set_custom_instructions",
            lambda ids: Rest("PUT", f"{s}/custom-instructions", {"text": "Keep it brief."}),
            lambda e, ids: _instructions(e, ids["story"]),
        ),
        Step(
            "custom_instructions",
            lambda ids: Rest("GET", f"{s}/custom-instructions"),
            lambda e, ids: _read_instructions(e, ids["story"]),
        ),
        # -- turns
        Step(
            "respond",
            lambda ids: Rest("POST", f"{s}/respond", _respond_request(ids)),
            lambda e, ids: _respond(e, ids),
            _keep("turn_operation", "beats", 0, "operation_id"),
        ),
        Step(
            "ingest",
            lambda ids: Rest("POST", f"{s}/ingest", _ingest_request()),
            lambda e, ids: _ingest(e, ids["story"]),
        ),
        Step(
            "respond",
            lambda ids: Rest("POST", f"{s}/respond", _respond_request(ids)),
            lambda e, ids: _respond(e, ids),
            _keep("last_turn", "turn"),
        ),
        Step(
            "history",
            lambda ids: Rest(
                "GET", f"{s}/history", params={"viewpoint": "omniscient", "include_audit": "1"}
            ),
            lambda e, ids: _history(e, ids["story"], viewpoint="omniscient", include_audit=True),
        ),
        Step(
            "get_operation",
            lambda ids: Rest(
                "GET",
                "/operations/{turn_operation}",
                params={"viewpoint": f"character:{ids['wren']}"},
            ),
            lambda e, ids: _operation(e, ids),
        ),
        # -- timeline
        Step(
            "regenerate",
            lambda ids: Rest("POST", f"{s}/regenerate", {"prompt_params": {"length": "short"}}),
            lambda e, ids: _regenerate(e, ids["story"]),
        ),
        Step(
            "variants",
            lambda ids: Rest("GET", f"{s}/variants"),
            lambda e, ids: _variants(e, ids["story"]),
        ),
        Step(
            "swap",
            lambda ids: Rest(
                "POST", f"{s}/swap", {"to_variant_chain": {"turn": ids["last_turn"], "variant": 1}}
            ),
            lambda e, ids: _swap(e, ids["story"], ids["last_turn"]),
        ),
        Step(
            "truncate",
            lambda ids: Rest("POST", f"{s}/truncate", {"to_turn": 1}),
            lambda e, ids: _truncate(e, ids["story"], 1),
        ),
        Step(
            "untruncate",
            lambda ids: Rest("POST", f"{s}/untruncate", {}),
            lambda e, ids: _untruncate(e, ids["story"]),
        ),
        Step(
            "history",
            lambda ids: Rest(
                "GET",
                f"{s}/history",
                params={"viewpoint": "omniscient", "variant": f"{ids['last_turn']}:2"},
            ),
            lambda e, ids: _history(
                e,
                ids["story"],
                viewpoint="omniscient",
                variant=ChainRef(turn=ids["last_turn"], variant=2),
            ),
        ),
        Step(
            "fork",
            lambda ids: Rest("POST", f"{s}/fork", {"at_turn": 1, "name": "Parity fork"}),
            lambda e, ids: _fork(e, ids["story"]),
            _keep("fork", "story_id"),
        ),
        Step(
            "rebuild_projections",
            lambda ids: Rest("POST", f"/admin{s}/rebuild"),
            lambda e, ids: _rebuild(e, ids["story"]),
        ),
        Step(
            "archive_story",
            lambda ids: Rest("POST", f"{s}/archive"),
            lambda e, ids: _status(e.archive_story(CTX, ids["story"])),
        ),
        Step(
            "unarchive_story",
            lambda ids: Rest("POST", f"{s}/unarchive"),
            lambda e, ids: _status(e.unarchive_story(CTX, ids["story"])),
        ),
        Step(
            "delete_story",
            lambda ids: Rest("DELETE", "/stories/{fork}"),
            lambda e, ids: _deleted(e, ids["fork"]),
        ),
        # -- ownership (#104): a story's owner and relations
        Step(
            "transfer_owner",
            lambda ids: Rest("PUT", f"{s}/owner", NEW_OWNER),
            lambda e, ids: _owner(
                e.transfer_owner(
                    CTX, EntityType.STORY, ids["story"], OwnerTransferRequest(**NEW_OWNER)
                )
            ),
        ),
        Step(
            "add_relation",
            lambda ids: Rest("POST", f"{s}/relations", RELATION),
            lambda e, ids: _relation(
                e.add_relation(CTX, EntityType.STORY, ids["story"], RelationRequest(**RELATION))
            ),
        ),
        Step(
            "list_relations",
            lambda ids: Rest("GET", f"{s}/relations"),
            lambda e, ids: _relations(e, EntityType.STORY, ids["story"]),
        ),
        Step(
            "list_stories",
            lambda ids: Rest(
                "GET",
                "/stories",
                params={"owner": "p-parity-owner", "relation": "co_author:p-parity-d"},
            ),
            lambda e, ids: _listed_by(e, owner="p-parity-owner", relation=["co_author:p-parity-d"]),
        ),
        Step(
            "remove_relation",
            lambda ids: Rest("DELETE", f"{s}/relations/co_author/p-parity-d"),
            lambda e, ids: _no_content(
                e.remove_relation(CTX, EntityType.STORY, ids["story"], "co_author", "p-parity-d")
            ),
        ),
        # -- error paths: the same error on both surfaces
        Step(
            "truncate",
            lambda ids: Rest("POST", f"{s}/truncate", {"to_turn": 99}),
            lambda e, ids: _truncate(e, ids["story"], 99),
        ),
        Step(
            "swap",
            lambda ids: Rest("POST", f"{s}/swap", {"to_variant_chain": {"turn": 9, "variant": 9}}),
            lambda e, ids: _swap(e, ids["story"], 9, variant=9),
        ),
        Step(
            "history",
            lambda ids: Rest("GET", f"{s}/history", params={"include_audit": "1"}),
            lambda e, ids: _history(e, ids["story"], include_audit=True),
        ),
        Step(
            "respond",
            lambda ids: Rest("POST", f"{s}/respond", _respond_request({"wren": "0" * 26})),
            lambda e, ids: _respond(e, {**ids, "wren": "0" * 26}),
        ),
        Step(
            "set_custom_instructions",
            lambda ids: Rest("PUT", f"{s}/custom-instructions", {"text": "a\x00b"}),
            lambda e, ids: _instructions(e, ids["story"], "a\x00b"),
        ),
        # -- the character catalog (#103)
        Step(
            "create_character_draft",
            lambda ids: Rest("POST", "/catalog/characters", CHARACTER),
            lambda e, ids: _draft(e.create_character_draft(CTX, CharacterCreate(**CHARACTER))),
            _keep("character", "id"),
        ),
        Step(
            "save_character_draft",
            lambda ids: Rest("PATCH", "/catalog/characters/{character}", CHARACTER_PATCH),
            lambda e, ids: _draft(
                e.save_character_draft(CTX, ids["character"], CharacterPatch(**CHARACTER_PATCH))
            ),
        ),
        Step(  # stale: the same patch again, from revision 1 (409 draft_stale on both)
            "save_character_draft",
            lambda ids: Rest("PATCH", "/catalog/characters/{character}", CHARACTER_PATCH),
            lambda e, ids: _draft(
                e.save_character_draft(CTX, ids["character"], CharacterPatch(**CHARACTER_PATCH))
            ),
        ),
        Step(
            "publish_character",
            lambda ids: Rest(
                "POST", "/catalog/characters/{character}/publish", {"draft_revision": 2}
            ),
            lambda e, ids: _published(
                e.publish_character(CTX, ids["character"], PublishRequest(draft_revision=2))
            ),
        ),
        Step(
            "get_character",
            lambda ids: Rest("GET", "/catalog/characters/{character}"),
            lambda e, ids: _character(e, ids["character"]),
        ),
        Step(
            "get_character",
            lambda ids: Rest(
                "GET",
                "/catalog/characters/{character}",
                params={"version": 1, "projection": "full"},
            ),
            lambda e, ids: _character(e, ids["character"], version=1, projection=Projection.FULL),
        ),
        Step(
            "get_character",
            lambda ids: Rest(
                "GET",
                "/catalog/characters/{character}",
                params={"draft": "true", "projection": "full"},
            ),
            lambda e, ids: _character(e, ids["character"], draft=True, projection=Projection.FULL),
        ),
        Step(
            "list_character_versions",
            lambda ids: Rest("GET", "/catalog/characters/{character}/versions"),
            lambda e, ids: _character_versions(e, ids["character"]),
        ),
        Step(
            "archive_character",
            lambda ids: Rest("POST", "/catalog/characters/{character}/archive"),
            lambda e, ids: _character_status(e.archive_character(CTX, ids["character"])),
        ),
        Step(
            "unarchive_character",
            lambda ids: Rest("POST", "/catalog/characters/{character}/unarchive"),
            lambda e, ids: _character_status(e.unarchive_character(CTX, ids["character"])),
        ),
        Step(
            "create_character_draft",
            lambda ids: Rest("POST", "/catalog/characters", GHOST),
            lambda e, ids: _draft(e.create_character_draft(CTX, CharacterCreate(**GHOST))),
            _keep("ghost", "id"),
        ),
        Step(  # never published: 422 not_published on both
            "get_character",
            lambda ids: Rest("GET", "/catalog/characters/{ghost}"),
            lambda e, ids: _character(e, ids["ghost"]),
        ),
        Step(
            "delete_character",
            lambda ids: Rest("DELETE", "/catalog/characters/{ghost}"),
            lambda e, ids: _no_content(e.delete_character(CTX, ids["ghost"])),
        ),
        # -- ownership (#104): a catalog character's owner and relations
        Step(
            "transfer_owner",
            lambda ids: Rest("PUT", "/catalog/characters/{character}/owner", NEW_OWNER),
            lambda e, ids: _owner(
                e.transfer_owner(
                    CTX, EntityType.CHARACTER, ids["character"], OwnerTransferRequest(**NEW_OWNER)
                )
            ),
        ),
        Step(
            "add_relation",
            lambda ids: Rest("POST", "/catalog/characters/{character}/relations", RELATION),
            lambda e, ids: _relation(
                e.add_relation(
                    CTX, EntityType.CHARACTER, ids["character"], RelationRequest(**RELATION)
                )
            ),
        ),
        Step(
            "list_relations",
            lambda ids: Rest("GET", "/catalog/characters/{character}/relations"),
            lambda e, ids: _relations(e, EntityType.CHARACTER, ids["character"]),
        ),
        # -- the scenario catalog (#108)
        Step(
            "create_scenario_draft",
            lambda ids: Rest("POST", "/catalog/scenarios", SCENARIO),
            lambda e, ids: _scenario_draft(
                e.create_scenario_draft(CTX, ScenarioCreate.model_validate(SCENARIO))
            ),
            _keep("scenario", "id"),
            group=ENTRY_GROUP,
        ),
        Step(
            "save_scenario_draft",
            lambda ids: Rest("PATCH", "/catalog/scenarios/{scenario}", SCENARIO_PATCH),
            lambda e, ids: _scenario_draft(
                e.save_scenario_draft(
                    CTX, ids["scenario"], ScenarioPatch.model_validate(SCENARIO_PATCH)
                )
            ),
            group=ENTRY_GROUP,
        ),
        Step(  # stale: the same patch again, from revision 1 (409 draft_stale on both)
            "save_scenario_draft",
            lambda ids: Rest("PATCH", "/catalog/scenarios/{scenario}", SCENARIO_PATCH),
            lambda e, ids: _scenario_draft(
                e.save_scenario_draft(
                    CTX, ids["scenario"], ScenarioPatch.model_validate(SCENARIO_PATCH)
                )
            ),
            group=ENTRY_GROUP,
        ),
        Step(
            "publish_scenario",
            lambda ids: Rest(
                "POST", "/catalog/scenarios/{scenario}/publish", {"draft_revision": 2}
            ),
            lambda e, ids: _scenario_published(
                e.publish_scenario(CTX, ids["scenario"], PublishRequest(draft_revision=2))
            ),
            group=ENTRY_GROUP,
        ),
        Step(
            "get_scenario",
            lambda ids: Rest("GET", "/catalog/scenarios/{scenario}"),
            lambda e, ids: _scenario(e, ids["scenario"]),
            group=ENTRY_GROUP,
        ),
        Step(
            "get_scenario",
            lambda ids: Rest(
                "GET",
                "/catalog/scenarios/{scenario}",
                params={"version": 1, "projection": "full"},
            ),
            lambda e, ids: _scenario(e, ids["scenario"], version=1, projection=Projection.FULL),
            group=ENTRY_GROUP,
        ),
        Step(
            "get_scenario",
            lambda ids: Rest(
                "GET",
                "/catalog/scenarios/{scenario}",
                params={"draft": "true", "projection": "full"},
            ),
            lambda e, ids: _scenario(e, ids["scenario"], draft=True, projection=Projection.FULL),
            group=ENTRY_GROUP,
        ),
        Step(
            "list_scenario_versions",
            lambda ids: Rest("GET", "/catalog/scenarios/{scenario}/versions"),
            lambda e, ids: _scenario_versions(e, ids["scenario"]),
            group=ENTRY_GROUP,
        ),
        Step(
            "archive_scenario",
            lambda ids: Rest("POST", "/catalog/scenarios/{scenario}/archive"),
            lambda e, ids: _scenario_status(e.archive_scenario(CTX, ids["scenario"])),
            group=ENTRY_GROUP,
        ),
        Step(
            "unarchive_scenario",
            lambda ids: Rest("POST", "/catalog/scenarios/{scenario}/unarchive"),
            lambda e, ids: _scenario_status(e.unarchive_scenario(CTX, ids["scenario"])),
            group=ENTRY_GROUP,
        ),
        Step(
            "create_scenario_draft",
            lambda ids: Rest("POST", "/catalog/scenarios", SCENARIO_GHOST),
            lambda e, ids: _scenario_draft(
                e.create_scenario_draft(CTX, ScenarioCreate.model_validate(SCENARIO_GHOST))
            ),
            _keep("ghost_scenario", "id"),
            group=ENTRY_GROUP,
        ),
        Step(  # never published: 422 not_published on both
            "get_scenario",
            lambda ids: Rest("GET", "/catalog/scenarios/{ghost_scenario}"),
            lambda e, ids: _scenario(e, ids["ghost_scenario"]),
            group=ENTRY_GROUP,
        ),
        Step(
            "delete_scenario",
            lambda ids: Rest("DELETE", "/catalog/scenarios/{ghost_scenario}"),
            lambda e, ids: _no_content(e.delete_scenario(CTX, ids["ghost_scenario"])),
            group=ENTRY_GROUP,
        ),
        Step(
            "transfer_owner",
            lambda ids: Rest("PUT", "/catalog/scenarios/{scenario}/owner", NEW_OWNER),
            lambda e, ids: _owner(
                e.transfer_owner(
                    CTX, EntityType.SCENARIO, ids["scenario"], OwnerTransferRequest(**NEW_OWNER)
                )
            ),
            group=ENTRY_GROUP,
        ),
        Step(
            "add_relation",
            lambda ids: Rest("POST", "/catalog/scenarios/{scenario}/relations", RELATION),
            lambda e, ids: _relation(
                e.add_relation(
                    CTX, EntityType.SCENARIO, ids["scenario"], RelationRequest(**RELATION)
                )
            ),
            group=ENTRY_GROUP,
        ),
        Step(
            "list_relations",
            lambda ids: Rest("GET", "/catalog/scenarios/{scenario}/relations"),
            lambda e, ids: _relations(e, EntityType.SCENARIO, ids["scenario"]),
            group=ENTRY_GROUP,
        ),
        # -- listing status and listings (#110): the character made `public`, then found by a
        # search with that status, and the scenarios listed by a label
        Step(
            "set_listing_status",
            lambda ids: Rest("PUT", "/catalog/characters/{character}/listing-status", PUBLIC),
            lambda e, ids: _listing_status(
                e.set_listing_status(
                    CTX, EntityType.CHARACTER, ids["character"], ListingStatusRequest(**PUBLIC)
                )
            ),
            group=LISTING_GROUP,
        ),
        Step(
            "search_catalog",
            lambda ids: Rest("GET", "/catalog/characters", params=CHARACTER_SEARCH),
            lambda e, ids: _characters_found(e, CHARACTER_SEARCH),
            group=LISTING_GROUP,
        ),
        Step(
            "search_catalog",
            lambda ids: Rest("GET", "/catalog/scenarios", params=SCENARIO_SEARCH),
            lambda e, ids: _scenarios_found(e, SCENARIO_SEARCH),
            group=LISTING_GROUP,
        ),
        # -- media assets (#105)
        Step(
            "upload_media",
            lambda ids: Rest("POST", "/media", files=files(MEDIA_BYTES, MEDIA)),
            lambda e, ids: _uploaded(e, MEDIA),
            _keep("media", "id"),
        ),
        Step(  # no depicted subjects: 422 subjects_required on both
            "upload_media",
            lambda ids: Rest("POST", "/media", files=files(MEDIA_BYTES, UNDECLARED)),
            lambda e, ids: _uploaded(e, UNDECLARED),
        ),
        Step(
            "get_media",
            lambda ids: Rest("GET", "/media/{media}"),
            lambda e, ids: _media(e, ids["media"]),
        ),
        Step(
            "get_media_content",
            lambda ids: Rest("GET", "/media/{media}/content"),
            lambda e, ids: _media_content(e, ids["media"]),
        ),
        Step(
            "list_media",
            lambda ids: Rest("GET", "/media", params={"kind": "image"}),
            lambda e, ids: _media_list(e),
        ),
        # -- media delivery (#107): a signed URL (its base set first), and resumable uploads
        Step(
            "update_deployment_settings",
            lambda ids: Rest("PUT", "/admin/settings", PUBLIC_BASE),
            lambda e, ids: _deployment_write(e, PUBLIC_BASE),
        ),
        Step(
            "get_media_url",
            lambda ids: Rest("GET", "/media/{media}/url", params={"ttl": 60}),
            lambda e, ids: _media_url(e, ids["media"]),
        ),
        Step(
            "open_upload",
            lambda ids: Rest("POST", "/media/uploads", SESSION),
            lambda e, ids: _upload_opened(e),
            _keep("upload", "upload_id"),
        ),
        Step(
            "put_upload_part",
            lambda ids: Rest("PUT", "/media/uploads/{upload}/parts/1", content=SESSION_BYTES),
            lambda e, ids: _part_stored(e, ids["upload"]),
        ),
        Step(
            "get_upload",
            lambda ids: Rest("GET", "/media/uploads/{upload}"),
            lambda e, ids: _upload(e, ids["upload"]),
        ),
        Step(
            "complete_upload",
            lambda ids: Rest("POST", "/media/uploads/{upload}/complete"),
            lambda e, ids: _completed(e, ids["upload"]),
        ),
        Step(
            "open_upload",
            lambda ids: Rest("POST", "/media/uploads", SESSION),
            lambda e, ids: _upload_opened(e),
            _keep("dropped", "upload_id"),
        ),
        Step(
            "abort_upload",
            lambda ids: Rest("DELETE", "/media/uploads/{dropped}"),
            lambda e, ids: _no_content(e.abort_upload(CTX, ids["dropped"])),
        ),
        Step(
            "transfer_owner",
            lambda ids: Rest("PUT", "/media/{media}/owner", NEW_OWNER),
            lambda e, ids: _owner(
                e.transfer_owner(
                    CTX, EntityType.MEDIA, ids["media"], OwnerTransferRequest(**NEW_OWNER)
                )
            ),
        ),
        Step(
            "delete_media",
            lambda ids: Rest("DELETE", "/media/{media}"),
            lambda e, ids: _no_content(e.delete_media(CTX, ids["media"])),
        ),
        Step(  # retired: still read
            "get_media",
            lambda ids: Rest("GET", "/media/{media}"),
            lambda e, ids: _media(e, ids["media"]),
        ),
        # -- engine profiles (#106)
        Step(
            "create_profile",
            lambda ids: Rest("POST", "/engine-profiles", PROFILE),
            lambda e, ids: _profile_write(e.create_profile(CTX, ProfileCreate(**PROFILE))),
            _keep("profile", "id"),
            group=PROFILE_GROUP,
        ),
        Step(
            "get_profile",
            lambda ids: Rest("GET", "/engine-profiles/{profile}"),
            lambda e, ids: _profile(e, ids["profile"]),
            group=PROFILE_GROUP,
        ),
        Step(
            "list_profiles",
            lambda ids: Rest("GET", "/engine-profiles", params={"owner": PRINCIPAL}),
            lambda e, ids: _profiles(e),
            group=PROFILE_GROUP,
        ),
        Step(
            "update_profile",
            lambda ids: Rest("PUT", "/engine-profiles/{profile}", PROFILE_EDIT),
            lambda e, ids: _profile_edit(
                e.update_profile(CTX, ids["profile"], ProfileUpdate(**PROFILE_EDIT))
            ),
            group=PROFILE_GROUP,
        ),
        Step(
            "get_profile",
            lambda ids: Rest("GET", "/engine-profiles/{profile}", params={"version": 1}),
            lambda e, ids: _profile(e, ids["profile"], version=1),
            group=PROFILE_GROUP,
        ),
        Step(
            "list_profile_versions",
            lambda ids: Rest("GET", "/engine-profiles/{profile}/versions"),
            lambda e, ids: _profile_versions(e, ids["profile"]),
            group=PROFILE_GROUP,
        ),
        Step(
            "copy_profile",
            lambda ids: Rest("POST", "/engine-profiles/{profile}/copy", PROFILE_COPY),
            lambda e, ids: _profile_write(
                e.copy_profile(CTX, ids["profile"], ProfileCopy(**PROFILE_COPY))
            ),
            _keep("profile_copy", "id"),
            group=PROFILE_GROUP,
        ),
        Step(
            "validate_profile",
            lambda ids: Rest("POST", "/engine-profiles/{profile}/validate", {}),
            lambda e, ids: _profile_check(e, ids["profile"], {}),
            group=PROFILE_GROUP,
        ),
        Step(  # no body: the latest version on both (the library takes no request)
            "validate_profile",
            lambda ids: Rest("POST", "/engine-profiles/{profile}/validate"),
            lambda e, ids: _profile_check(e, ids["profile"], None),
            group=PROFILE_GROUP,
        ),
        Step(  # an undelivered level: 422 invalid_profile on both
            "validate_profile",
            lambda ids: Rest("POST", "/engine-profiles/{profile}/validate", PROFILE_DRY_RUN),
            lambda e, ids: _profile_check(e, ids["profile"], PROFILE_DRY_RUN),
            group=PROFILE_GROUP,
        ),
        Step(
            "profile_schema",
            lambda ids: Rest("GET", "/engine-profiles/schema"),
            lambda e, ids: _profile_schema(e),
            group=PROFILE_GROUP,
        ),
        Step(  # the deployment's default: 409 on both
            "delete_profile",
            lambda ids: Rest("DELETE", f"/engine-profiles/{LITE_PRESET_ID}"),
            lambda e, ids: _profile_deleted(e.delete_profile(CTX, LITE_PRESET_ID)),
            group=PROFILE_GROUP,
        ),
        Step(
            "delete_profile",
            lambda ids: Rest("DELETE", "/engine-profiles/{profile_copy}"),
            lambda e, ids: _profile_deleted(e.delete_profile(CTX, ids["profile_copy"])),
            group=PROFILE_GROUP,
        ),
        Step(
            "transfer_owner",
            lambda ids: Rest("PUT", "/engine-profiles/{profile}/owner", NEW_OWNER),
            lambda e, ids: _owner(
                e.transfer_owner(
                    CTX,
                    EntityType.ENGINE_PROFILE,
                    ids["profile"],
                    OwnerTransferRequest(**NEW_OWNER),
                )
            ),
            group=PROFILE_GROUP,
        ),
        # -- a story's configuration (#109): a preview that writes nothing, and the read
        Step(
            "impact_preview",
            lambda ids: Rest("POST", f"{s}/impact-preview", {"profile_id": ids["profile"]}),
            lambda e, ids: _impact(e, ids["story"], {"profile_id": ids["profile"]}),
            group=PROFILE_GROUP,
        ),
        Step(
            "get_effective_config",
            lambda ids: Rest("GET", f"{s}/effective-config"),
            lambda e, ids: _effective(e, ids["story"]),
            group=PROFILE_GROUP,
        ),
        # -- the operator surface
        Step(
            "list_content_ratings",
            lambda ids: Rest("GET", "/admin/content-ratings"),
            lambda e, ids: _ratings(e),
        ),
        Step(
            "get_content_rating",
            lambda ids: Rest("GET", f"/admin/content-ratings/{EVERYONE_ID}"),
            lambda e, ids: _rating(e, EVERYONE_ID),
        ),
        Step(
            "create_content_rating",
            lambda ids: Rest("POST", "/admin/content-ratings", RATING),
            lambda e, ids: _rating_write(e.create_content_rating(CTX, RatingFields(**RATING))),
            _keep("rating", "id"),
        ),
        Step(
            "update_content_rating",
            lambda ids: Rest("PUT", "/admin/content-ratings/{rating}", _rating_replacement()),
            lambda e, ids: _rating_write(
                e.update_content_rating(
                    CTX, ids["rating"], RatingReplacement(**_rating_replacement())
                )
            ),
        ),
        Step(
            "content_rating_versions",
            lambda ids: Rest("GET", "/admin/content-ratings/{rating}/versions"),
            lambda e, ids: _versions(e, ids["rating"]),
        ),
        Step(
            "retire_content_rating",
            lambda ids: Rest("DELETE", "/admin/content-ratings/{rating}"),
            lambda e, ids: _rating_write(e.retire_content_rating(CTX, ids["rating"])),
        ),
        Step(
            "deployment_settings",
            lambda ids: Rest("GET", "/admin/settings"),
            lambda e, ids: _deployment(e),
        ),
        Step(
            "update_deployment_settings",
            lambda ids: Rest("PUT", "/admin/settings", {"lite_window_messages": 40}),
            lambda e, ids: _deployment_write(e, {"lite_window_messages": 40}),
        ),
        Step(
            "config_changes",
            lambda ids: Rest("GET", "/admin/config-changes"),
            lambda e, ids: _changes(e, ConfigScope.DEPLOYMENT, None),
        ),
        Step(
            "policies_list",
            lambda ids: Rest("GET", "/admin/policies", params={"scope": "global"}),
            lambda e, ids: _policies(e),
        ),
        Step(
            "policies_get",
            lambda ids: Rest("GET", f"/admin/policies/{MINORS_POLICY_ID}"),
            lambda e, ids: _policy(e, MINORS_POLICY_ID),
        ),
        Step(
            "policies_create",
            lambda ids: Rest("POST", "/admin/policies", GLOBAL_POLICY),
            lambda e, ids: _policy_write(e.policies_create(CTX, PolicyFields(**GLOBAL_POLICY))),
            _keep("policy", "id"),
        ),
        Step(
            "policies_update",
            lambda ids: Rest("PUT", "/admin/policies/{policy}", _policy_replacement()),
            lambda e, ids: _policy_write(
                e.policies_update(CTX, ids["policy"], PolicyReplacement(**_policy_replacement()))
            ),
        ),
        Step(
            "policies_retire",
            lambda ids: Rest("DELETE", "/admin/policies/{policy}"),
            lambda e, ids: _policy_write(e.policies_retire(CTX, ids["policy"])),
        ),
        Step(
            "model_configuration",
            lambda ids: Rest("GET", "/admin/model-config"),
            lambda e, ids: _model_configuration(e),
        ),
        Step(
            "replace_model_config",
            lambda ids: Rest("PUT", "/admin/model-config/generation", MODEL_OVERRIDE),
            lambda e, ids: _model_replace(e),
        ),
        Step(
            "model_config_entry",
            lambda ids: Rest("GET", "/admin/model-config/generation"),
            lambda e, ids: _model_entry(e, "generation"),
        ),
        Step(
            "set_model_api_key",
            lambda ids: Rest("PUT", "/admin/model-config/generation/api-key", {"api_key": "k-1"}),
            lambda e, ids: _no_content(e.set_model_api_key(CTX, "generation", SecretStr("k-1"))),
        ),
        Step(
            "remove_model_api_key",
            lambda ids: Rest("DELETE", "/admin/model-config/generation/api-key"),
            lambda e, ids: _no_content(e.remove_model_api_key(CTX, "generation")),
        ),
        Step(
            "remove_model_override",
            lambda ids: Rest("DELETE", "/admin/model-config/generation"),
            lambda e, ids: _model_remove(e),
        ),
    ]


# -- the library renderings ----------------------------------------------------------------------


async def _created(engine: Engine) -> dict[str, Any]:
    spec = StorySpec.model_validate(story_body("Wren", "Ada"))
    return dump(StoryCreatedBody.of(await engine.create_story(CTX, spec)))


async def _listed(engine: Engine) -> dict[str, Any]:
    from epistrel.core.enums import StoryStatus

    return dump(StoryList.of(await engine.list_stories(CTX, status=StoryStatus.ACTIVE)))


async def _listed_by(engine: Engine, **filters: Any) -> dict[str, Any]:
    return dump(StoryList.of(await engine.list_stories(CTX, **filters)))


async def _owner(call: Awaitable[Any]) -> dict[str, Any]:
    transfer = await call
    model = StoryOwnerBody if transfer.entity_type is EntityType.STORY else OwnerBody
    return dump(model.of(transfer))


async def _relation(call: Awaitable[Any]) -> dict[str, Any]:
    write = await call
    model = StoryRelationBody if write.entity_type is EntityType.STORY else RelationBody
    return dump(model.of(write))


async def _relations(engine: Engine, entity_type: EntityType, entity_id: str) -> dict[str, Any]:
    return dump(RelationsBody.of(await engine.list_relations(CTX, entity_type, entity_id)))


async def _changes(engine: Engine, scope: ConfigScope, story_id: str | None) -> dict[str, Any]:
    page = await engine.config_changes(CTX, scope=scope, story_id=story_id)
    return dump(ConfigChangesBody.of(page))


async def _pins(engine: Engine, story_id: str) -> dict[str, Any]:
    request = PinnedMemoriesRequest.model_validate({"items": [{"text": "Ada owes Wren."}]})
    return dump(PinnedMemoriesWritten.of(await engine.set_pinned_memories(CTX, story_id, request)))


async def _read_pins(engine: Engine, story_id: str) -> dict[str, Any]:
    return dump(PinnedMemoriesBody.of(await engine.pinned_memories(CTX, story_id)))


async def _instructions(
    engine: Engine, story_id: str, text: str = "Keep it brief."
) -> dict[str, Any]:
    request = CustomInstructionsRequest(text=text)
    written = await engine.set_custom_instructions(CTX, story_id, request)
    return dump(CustomInstructionsWritten.of(written))


async def _read_instructions(engine: Engine, story_id: str) -> dict[str, Any]:
    return dump(CustomInstructionsBody.of(await engine.custom_instructions(CTX, story_id)))


async def _respond(engine: Engine, ids: Ids) -> dict[str, Any]:
    request = RespondRequest.model_validate(_respond_request(ids))
    return dump(RespondResponse.of(await engine.respond(CTX, ids["story"], request)))


async def _ingest(engine: Engine, story_id: str) -> dict[str, Any]:
    request = IngestRequest.model_validate(_ingest_request())
    return dump(IngestResponse.of(await engine.ingest(CTX, story_id, request)))


async def _history(engine: Engine, story_id: str, **params: Any) -> dict[str, Any]:
    found = await engine.history(RequestContext(), story_id, **params)
    return dump(HistoryResponse.of(found), exclude_unset=True)


async def _operation(engine: Engine, ids: Ids) -> dict[str, Any]:
    viewpoint = f"character:{ids['wren']}"
    view = await engine.get_operation(CTX, ids["turn_operation"], viewpoint=viewpoint)
    return dump(OperationBody.of(view))


async def _regenerate(engine: Engine, story_id: str) -> dict[str, Any]:
    request = RegenerateRequest.model_validate({"prompt_params": {"length": "short"}})
    result = await engine.regenerate(CTX, story_id, request)
    return dump(RegeneratedBody.of_regenerate(result))


async def _variants(engine: Engine, story_id: str) -> dict[str, Any]:
    return dump(VariantsBody.of(await engine.variants(CTX, story_id)))


async def _swap(engine: Engine, story_id: str, turn: int, *, variant: int = 1) -> dict[str, Any]:
    request = SwapRequest.model_validate({"to_variant_chain": {"turn": turn, "variant": variant}})
    return dump(SwappedBody.of(await engine.swap(CTX, story_id, request)))


async def _truncate(engine: Engine, story_id: str, to_turn: int) -> dict[str, Any]:
    done = await engine.truncate(CTX, story_id, TruncateRequest(to_turn=to_turn))
    return dump(TruncatedBody.of(done))


async def _untruncate(engine: Engine, story_id: str) -> dict[str, Any]:
    return dump(UntruncatedBody.of(await engine.untruncate(CTX, story_id, UntruncateRequest())))


async def _fork(engine: Engine, story_id: str) -> dict[str, Any]:
    request = ForkRequest(at_turn=1, name="Parity fork")
    return dump(ForkedBody.of(await engine.fork(CTX, story_id, request)))


async def _status(call: Awaitable[Any]) -> dict[str, Any]:
    return dump(StatusBody.of(await call))


async def _deleted(engine: Engine, story_id: str) -> dict[str, Any]:
    await engine.delete_story(CTX, story_id)
    return {}


async def _no_content(call: Awaitable[Any]) -> dict[str, Any]:
    await call
    return {}


async def _draft(call: Awaitable[Any]) -> dict[str, Any]:
    return dump(CharacterDraftBody.of(await call))


async def _published(call: Awaitable[Any]) -> dict[str, Any]:
    return dump(CharacterPublishedBody.of(await call))


async def _character_status(call: Awaitable[Any]) -> dict[str, Any]:
    return dump(CharacterStatusBody.of(await call))


async def _character(engine: Engine, character_id: str, **params: Any) -> dict[str, Any]:
    return dump(await engine.get_character(CTX, character_id, **params))


async def _character_versions(engine: Engine, character_id: str) -> dict[str, Any]:
    found = await engine.list_character_versions(CTX, character_id)
    return dump(CharacterVersionsBody.of(found))


async def _scenario_draft(call: Awaitable[Any]) -> dict[str, Any]:
    return dump(ScenarioDraftBody.of(await call))


async def _scenario_published(call: Awaitable[Any]) -> dict[str, Any]:
    return dump(ScenarioPublishedBody.of(await call))


async def _scenario_status(call: Awaitable[Any]) -> dict[str, Any]:
    return dump(ScenarioStatusBody.of(await call))


async def _scenario(engine: Engine, scenario_id: str, **params: Any) -> dict[str, Any]:
    return dump(await engine.get_scenario(CTX, scenario_id, **params))


async def _scenario_versions(engine: Engine, scenario_id: str) -> dict[str, Any]:
    return dump(ScenarioVersionsBody.of(await engine.list_scenario_versions(CTX, scenario_id)))


async def _listing_status(call: Awaitable[Any]) -> dict[str, Any]:
    return dump(ListingStatusBody.of(await call))


async def _characters_found(engine: Engine, filters: dict[str, Any]) -> dict[str, Any]:
    page = await engine.search_catalog(CTX, EntityType.CHARACTER, **filters)
    return dump(CharacterListBody.of(page))


async def _scenarios_found(engine: Engine, filters: dict[str, Any]) -> dict[str, Any]:
    page = await engine.search_catalog(CTX, EntityType.SCENARIO, **filters)
    return dump(ScenarioListBody.of(page))


async def _uploaded(engine: Engine, fields: dict[str, Any]) -> dict[str, Any]:
    request = MediaUpload.model_validate(fields)
    written = await engine.upload_media(CTX, request, MEDIA_BYTES, content_type="image/png")
    return dump(MediaUploadedBody.of(written))


async def _media(engine: Engine, media_id: str) -> dict[str, Any]:
    return dump(await engine.get_media(CTX, media_id))


async def _media_content(engine: Engine, media_id: str) -> dict[str, Any]:
    content = await engine.get_media_content(CTX, media_id)
    return _bytes(await content.read(), content.mime_type)


async def _media_url(engine: Engine, media_id: str) -> dict[str, Any]:
    return dump(MediaUrlBody.of(await engine.get_media_url(CTX, media_id, ttl=60)))


async def _upload_opened(engine: Engine) -> dict[str, Any]:
    opened = await engine.open_upload(CTX, UploadOpen.model_validate(SESSION))
    return dump(UploadOpenedBody.of(opened))


async def _part_stored(engine: Engine, upload_id: str) -> dict[str, Any]:
    stored = await engine.put_upload_part(CTX, upload_id, 1, SESSION_BYTES)
    return dump(PartStoredBody.of(stored))


async def _upload(engine: Engine, upload_id: str) -> dict[str, Any]:
    return dump(await engine.get_upload(CTX, upload_id))


async def _completed(engine: Engine, upload_id: str) -> dict[str, Any]:
    return dump(MediaUploadedBody.of(await engine.complete_upload(CTX, upload_id)))


async def _media_list(engine: Engine) -> dict[str, Any]:
    from epistrel.core.tables.media import MediaKind

    return dump(MediaListBody.of(await engine.list_media(CTX, kind=MediaKind.IMAGE)))


async def _profile_write(call: Awaitable[Any]) -> dict[str, Any]:
    return dump(ProfileBody.of(await call))


async def _profile_edit(call: Awaitable[Any]) -> dict[str, Any]:
    return dump(ProfileEditBody.of(await call))


async def _impact(engine: Engine, story_id: str, body: dict[str, Any]) -> dict[str, Any]:
    request = ImpactPreviewRequest.model_validate(body)
    return dump(await engine.impact_preview(CTX, story_id, request))


async def _effective(engine: Engine, story_id: str) -> dict[str, Any]:
    return dump(await engine.get_effective_config(CTX, story_id))


async def _profile(engine: Engine, profile_id: str, **params: Any) -> dict[str, Any]:
    return dump(await engine.get_profile(CTX, profile_id, **params))


async def _profiles(engine: Engine) -> dict[str, Any]:
    return dump(ProfileListBody.of(await engine.list_profiles(CTX, owner=PRINCIPAL)))


async def _profile_versions(engine: Engine, profile_id: str) -> dict[str, Any]:
    found = await engine.list_profile_versions(CTX, profile_id)
    return dump(ProfileVersionsBody(items=found))


async def _profile_check(
    engine: Engine, profile_id: str, body: dict[str, Any] | None
) -> dict[str, Any]:
    request = None if body is None else ProfileValidate.model_validate(body)
    return dump(await engine.validate_profile(CTX, profile_id, request))


async def _profile_schema(engine: Engine) -> dict[str, Any]:
    return dump(ProfileSchemaBody.of(await engine.profile_schema(CTX)))


async def _profile_deleted(call: Awaitable[Any]) -> dict[str, Any]:
    return dump(ProfileDeletedBody.of(await call))


def _bytes(data: bytes, content_type: str) -> dict[str, Any]:
    """A content read as compared: the bytes' digest and type."""
    return {"content_sha256": hashlib.sha256(data).hexdigest(), "content_type": content_type}


async def _ratings(engine: Engine) -> dict[str, Any]:
    return dump(RatingList.of(await engine.list_content_ratings(CTX)))


async def _rating(engine: Engine, rating_id: str) -> dict[str, Any]:
    return dump(RatingDetailBody.of_detail(await engine.get_content_rating(CTX, rating_id)))


async def _versions(engine: Engine, rating_id: str) -> dict[str, Any]:
    return dump(RatingList.of(await engine.content_rating_versions(CTX, rating_id)))


async def _rating_write(call: Awaitable[Any]) -> dict[str, Any]:
    write = await call
    return dump(RatingMutation.written(write)) if write.changed else {}


def _rating_replacement() -> dict[str, Any]:
    return {
        **RATING,
        "description": "Changed by C-03.",
        "display_order": 990,
        "prompt_guidance": "",
        "image_guidance": "",
        "policy_ids": [],
    }


async def _deployment(engine: Engine) -> dict[str, Any]:
    return dump(SettingsBody.of(await engine.deployment_settings(CTX)))


async def _deployment_write(engine: Engine, patch: dict[str, Any]) -> dict[str, Any]:
    state = await engine.update_deployment_settings(CTX, SettingsPatch.model_validate(patch))
    return dump(SettingsBody.of(state))


async def _policies(engine: Engine) -> dict[str, Any]:
    return dump(PolicyList.of(await engine.policies_list(CTX, scope=Scope.GLOBAL)))


async def _policy(engine: Engine, policy_id: str) -> dict[str, Any]:
    return dump(PolicyBody.of(await engine.policies_get(CTX, policy_id)))


async def _policy_write(call: Awaitable[Any]) -> dict[str, Any]:
    write = await call
    if not write.changed:
        return {}
    return dump(PolicyMutation.written(write.policy, write.config_revision))


def _policy_replacement() -> dict[str, Any]:
    return {
        "name": "Parity global, edited",
        "applies_to": ["text_output"],
        "rule": {"type": "wordlist", "words": ["zzqx", "qxzz"]},
        "age_condition": None,
        "message": "Never say zzqx or qxzz.",
    }


async def _model_configuration(engine: Engine) -> dict[str, Any]:
    revision, entries = await engine.model_configuration(CTX)
    return dump(ModelConfigBody.of(engine.settings.model_adapter, revision, entries))


async def _model_entry(engine: Engine, purpose: str) -> dict[str, Any]:
    revision, entry = await engine.model_config_entry(CTX, purpose)
    fields = ModelConfigEntry.fields_of(entry)
    return dump(ModelConfigItem(purpose=purpose, config_revision=revision, **fields))


async def _model_replace(engine: Engine) -> dict[str, Any]:
    body = ConfigReplacement.model_validate(MODEL_OVERRIDE)
    write = await engine.replace_model_config(CTX, "generation", body)
    return await _model_config_write(engine, "generation", write)


async def _model_remove(engine: Engine) -> dict[str, Any]:
    write = await engine.remove_model_override(CTX, "generation")
    return await _model_config_write(engine, "generation", write)


# -- running the sequence --------------------------------------------------------------------------


def normalise(value: Any, normalizer: Normalizer) -> Any:
    """``value`` with ids by position, timestamps and durations blanked, transport fields gone."""
    if isinstance(value, dict):
        return {
            normalizer(key): 0 if key in ZEROED else normalise(item, normalizer)
            for key, item in value.items()
            if key not in DROPPED
        }
    if isinstance(value, list):
        return [normalise(item, normalizer) for item in value]
    if isinstance(value, str) and TIMESTAMP.match(value):
        return "<time>"
    if isinstance(value, str) and FINGERPRINT.match(value):
        return normalizer.placeholder(value)
    if isinstance(value, str):
        value = SIGNED.sub(r"\1<signed>", value)
    return normalizer(value)


def error_body(error: EpistrelError) -> dict[str, Any]:
    return {"error": {"code": error.code, "message": error.message, "details": error.details}}


@asynccontextmanager
async def surface_engine(settings: Settings) -> AsyncIterator[tuple[Engine, FastAPI]]:
    """An Engine bound to the surface's own database, and an app serving it."""
    db = make_engine(settings)
    app = create_app(settings)
    engine: Engine = app.state.epistrel
    engine.bind(db)
    assert engine.models.stub is not None
    engine.models.stub.when("", REPLY, purpose="generation")
    try:
        yield engine, app
    finally:
        await engine.drainer.stop()
        await engine.models.aclose()
        await db.dispose()


async def run_rest(settings: Settings, steps: list[Step]) -> list[Rendered]:
    out: list[Rendered] = []
    ids: Ids = {}
    headers = bearer() | {"X-Epistrel-Principal": PRINCIPAL}
    async with surface_engine(settings) as (_, app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for step in steps:
                call = step.rest(ids)
                response = await client.request(
                    call.method,
                    call.path.format(**ids),
                    json=call.json,
                    params=call.params,
                    files=call.files,
                    content=call.content,
                    headers=headers,
                )
                kind = response.headers.get("content-type", "application/json")
                if response.content and not kind.startswith("application/json"):
                    body = _bytes(response.content, kind)
                else:
                    body = response.json() if response.content else {}
                if response.is_success:
                    step.capture(body, ids)
                out.append((response.status_code, body))
    return out


async def run_library(settings: Settings, steps: list[Step]) -> list[Rendered]:
    out: list[Rendered] = []
    ids: Ids = {}
    async with surface_engine(settings) as (engine, app):
        statuses = _statuses(app)
        for step in steps:
            try:
                body = await step.library(engine, ids)
            except EpistrelError as error:
                out.append((error.http_status, error_body(error)))
                continue
            step.capture(body, ids)
            out.append((statuses[step.operation](body), body))
    return out


def _statuses(app: FastAPI) -> dict[str, Callable[[dict[str, Any]], int]]:
    """Each operation's success status as its route answers it: the route's status code, 204
    for a write that changed nothing (an empty body here)."""
    codes: dict[str, int] = {}
    for route in api_routes(app):
        if route.operation_id:
            code = route.status_code or 200
            codes[route.operation_id] = code
            for operation in operations():
                if route.operation_id.startswith(f"{operation}_"):
                    codes.setdefault(operation, code)
    return {name: _status_of(code) for name, code in codes.items()}


def api_routes(app: FastAPI) -> list[APIRoute]:
    """The app's routes as served (FastAPI includes routers lazily)."""
    return [
        context.original_route
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute)
    ]


def _status_of(code: int) -> Callable[[dict[str, Any]], int]:
    def status(body: dict[str, Any]) -> int:
        return code if body or code == 204 else 204

    return status


def parity_gaps(engine_class: type[Engine], app: FastAPI, steps: list[Step]) -> list[str]:
    """Engine operations that no route serves or no step runs."""
    routed = {route.operation_id for route in api_routes(app) if route.operation_id}
    stepped = {step.operation for step in steps}
    gaps: list[str] = []
    for name in operations(engine_class):
        has_route = name in routed or any(op.startswith(f"{name}_") for op in routed)
        if not has_route or name not in stepped:
            gaps.append(name)
    return gaps


@pytest.fixture(scope="module")
def surfaces(pg_url: str) -> Any:
    with (
        migrated_database(pg_url, "epistrel_c03_rest") as rest_url,
        migrated_database(pg_url, "epistrel_c03_lib") as lib_url,
    ):
        yield (
            auth_settings(rest_url, model_adapter="stub", config_key=config_key()),
            auth_settings(lib_url, model_adapter="stub", config_key=config_key()),
        )


def differences(rest: list[Rendered], library: list[Rendered], steps: list[Step]) -> list[str]:
    left, right = Normalizer(), Normalizer()
    found: list[str] = []
    for index, (step, one, two) in enumerate(zip(steps, rest, library, strict=True)):
        a = (one[0], normalise(one[1], left))
        b = (two[0], normalise(two[1], right))
        if a != b:
            found.append(f"step {index} {step.label}:\n  rest    {a}\n  library {b}")
    return found


@pytest.mark.integration
@pytest.mark.part("M2", "FR-API-3")
async def test_c_03(surfaces: tuple[Settings, Settings], dummy_dsn: str) -> None:
    """The same calls give the same results over REST and in-process, for every operation.

    Complement asserted: the comparison sees real results (the turn's reply text came back on
    both surfaces, the error steps answered errors, not successes, and each character and media
    step its intended status)."""
    steps = _steps()
    app = create_app(auth_settings(dummy_dsn))
    assert parity_gaps(Engine, app, steps) == []
    rest = await run_rest(surfaces[0], steps)
    library = await run_library(surfaces[1], steps)
    assert differences(rest, library, steps) == []
    replies = [b["message"]["text"] for _, b in rest if isinstance(b.get("message"), dict)]
    assert replies
    assert all(text == REPLY for text in replies)
    assert sorted({status for status, _ in rest if status >= 400}) == [403, 409, 422]
    # The character steps (#103) answered what they were written to answer, on both surfaces.
    answered = [
        (step.operation, status)
        for step, (status, _) in zip(steps, rest, strict=True)
        if "character" in step.operation
    ]
    assert answered == [
        ("create_character_draft", 201),
        ("save_character_draft", 200),
        ("save_character_draft", 409),
        ("publish_character", 201),
        ("get_character", 200),
        ("get_character", 200),
        ("get_character", 200),
        ("list_character_versions", 200),
        ("archive_character", 200),
        ("unarchive_character", 200),
        ("create_character_draft", 201),
        ("get_character", 422),
        ("delete_character", 204),
    ]
    # The media steps (#105) too (``MEDIA_ANSWERS``), and the session steps' (#107).
    media = [
        (step.operation, status)
        for step, (status, _) in zip(steps, rest, strict=True)
        if "media" in step.operation
    ]
    assert media == MEDIA_ANSWERS
    sessions = [
        (step.operation, status)
        for step, (status, _) in zip(steps, rest, strict=True)
        if step.operation in SESSION_OPERATIONS
    ]
    assert sessions == SESSION_ANSWERS
    entries = [
        (step.operation, status)
        for step, (status, _) in zip(steps, rest, strict=True)
        if step.group == ENTRY_GROUP
    ]
    assert entries == ENTRY_ANSWERS
    listings = [
        (step.operation, status, body)
        for step, (status, body) in zip(steps, rest, strict=True)
        if step.group == LISTING_GROUP
    ]
    assert [(operation, status) for operation, status, _ in listings] == LISTING_ANSWERS
    found = [len(body["items"]) for _, _, body in listings if "items" in body]
    assert found == [1, 1], "each list found its one entry"
    profiles = [
        (step.operation, status)
        for step, (status, _) in zip(steps, rest, strict=True)
        if step.group == PROFILE_GROUP
    ]
    assert profiles == PROFILE_ANSWERS


def test_c_03_bites_on_a_method_without_a_route_or_step(dummy_dsn: str) -> None:
    """A new public Engine method with no route (and no parity step) fails the parity list."""

    class Wider(Engine):
        async def brand_new_operation(self, ctx: RequestContext) -> None:
            """Library only, by mistake."""

    app = create_app(auth_settings(dummy_dsn))
    assert parity_gaps(Wider, app, _steps()) == ["brand_new_operation"]
    assert parity_gaps(Engine, app, _steps()[:-1]) == ["remove_model_override"]
