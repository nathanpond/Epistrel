"""C-06: external-provider conformance (docs/04; FR-EXT-1,2,5,6), the M6 parts #204 builds:
registration and runtime registration updates (``test_c_06__registration``), and each story's
binding to its providers (``test_c_06__bindings``). The live-read, verification-routing and
untrusted-content clauses are #205's and #207's parts of the same test ID.

The C-06 conformance helpers (``conformance_read``) run the same assertions over both adapters
(C-07's M6 part, ``test_c_07.py``)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import OperationKind
from epistrel.core.ids import new_ulid
from epistrel.core.states import verify as sv
from epistrel.engine import Engine
from epistrel.orchestrator.providers import registry
from epistrel.orchestrator.providers.port import Binding, ProviderUnavailable
from tests import canon_support as canon
from tests import effect_support as es
from tests import genesis_support as g
from tests import multi_support as ms
from tests import provider_support as ps
from tests import state_support as ss
from tests.auth_support import auth_settings
from tests.event_support import rows
from tests.respond_support import body as respond_body
from tests.respond_support import respond_client
from tests.story_support import READ, WRITE, create, story_body
from tests.structured_turn_support import beat, structured_story, turn_report

pytestmark = pytest.mark.integration

ADMIN = "/admin/external-providers"
SECRET_ENV = "EPISTREL_PROVIDER_SECRET_BANK"
SECRET = "s3cr3t-token-never-shown"


def _error(response: Any) -> tuple[int, str, dict[str, Any]]:
    error = response.json()["error"]
    return response.status_code, error["code"], error["details"]


async def _read(
    engine: AsyncEngine, lib: Engine, name: str, binding: Binding, value: str = "gold"
) -> Any:
    async with engine.connect() as conn:
        provider = await registry.provider_for(conn, name, in_process=lib.in_process_providers)
    return await provider.read(value, binding, None)


async def _config_rows(engine: AsyncEngine, name: str) -> list[Any]:
    return list(
        await rows(
            engine,
            "SELECT setting, class, old, new, config_revision FROM config_changes "
            "WHERE scope = 'deployment' AND setting LIKE :s ORDER BY change_id",
            s=f"external_provider:{name}%",
        )
    )


@pytest.mark.part("M6", "FR-EXT-1,6")
async def test_c_06__registration(
    pg_url: str,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    states_delivered: None,
) -> None:
    """An operator registers a declarative HTTP manifest over ``/admin/external-providers``, and
    a story's read substitutes its binding and the secret and reads the value at the manifest's
    path, with no code written. A ``PUT`` takes effect at the next resolution without a restart,
    while a provider resolved before it keeps its registration; every write is one interpretive
    ``external_provider:<name>[.<field>]`` change. ``GET`` never shows the secret.

    Complements asserted: a manifest with an unknown placeholder, a placeholder in the host, a
    disallowed host, a non-loopback ``http`` URL or a filter expression in ``value_path`` is
    refused 422 ``invalid_manifest`` with nothing written; a binding id holding ``/`` and ``?``
    is percent-encoded and the request stays on the allowed host; a 302 to another host is not
    followed; deleting a provider a story's State names is 409 ``provider_in_use``."""
    monkeypatch.setenv(SECRET_ENV, SECRET)
    settings = auth_settings(pg_url)
    name = ps.unique_name("bank")
    with ps.stub_server() as stub:
        async with (
            ps.allowed_hosts(engine, [stub.host]),
            respond_client(settings, engine) as (
                client,
                lib,
            ),
        ):
            read = ps.read_template(
                stub.base_url,
                "/players/{binding.player}/gold",
                headers={"Authorization": "Bearer {secret.BANK}"},
            )
            body = ps.http_spec(stub.base_url, name, read=read)

            # Refusals first: each writes nothing.
            revision0 = (await client.get(ADMIN, headers=READ)).json()["config_revision"]
            refusals = {
                "unknown_placeholder": ps.http_spec(
                    stub.base_url, name, read={**read, "url": f"{stub.base_url}/p/{{player}}"}
                ),
                "placeholder_in_authority": ps.http_spec(
                    stub.base_url, name, read={**read, "url": "http://{binding.host}/gold"}
                ),
                "host_not_allowed": ps.http_spec(
                    "https://api.elsewhere.test",
                    name,
                    read={**read, "url": "https://api.elsewhere.test/g"},
                ),
                "scheme": ps.http_spec(
                    stub.base_url,
                    name,
                    read={
                        **read,
                        "url": f"http://{stub.host.replace('127.0.0.1', 'api.bank.test')}/g",
                    },
                ),
                "path_expression": ps.http_spec(
                    stub.base_url,
                    name,
                    read={**read, "value_path": "$.accounts[?(@.main)].balance"},
                ),
            }
            for reason, refused_body in refusals.items():
                response = await client.post(ADMIN, json=refused_body, headers=WRITE)
                status, code, details = _error(response)
                assert (status, code, details["reason"]) == (422, "invalid_manifest", reason)
                assert details["field"].startswith("body.manifest.values.gold.read.")
            listed = (await client.get(ADMIN, headers=READ)).json()
            assert listed["config_revision"] == revision0
            assert name not in {p["name"] for p in listed["providers"]}
            assert await _config_rows(engine, name) == []

            # Registered: 201, the manifest as registered (the secret reference, not the secret).
            made = await client.post(ADMIN, json=body, headers=WRITE)
            assert made.status_code == 201, made.text
            registered = made.json()
            assert registered["config_revision"] == revision0 + 1
            assert registered["manifest"]["values"]["gold"]["read"]["headers"] == {
                "Authorization": "Bearer {secret.BANK}"
            }
            again = await client.post(ADMIN, json=body, headers=WRITE)
            assert _error(again)[:2] == (409, "provider_exists")
            shown = [
                (await client.get(f"{ADMIN}/{name}", headers=READ)).text,
                (await client.get(ADMIN, headers=READ)).text,
                json.dumps(
                    [dict(r._mapping) for r in await _config_rows(engine, name)], default=str
                ),
            ]
            assert all(SECRET not in text for text in shown)
            assert "{secret.BANK}" in shown[0]

            # A story reads its own player's value: the id substituted (percent-encoded), the
            # secret sent, the value at ``$.balance``.
            story = new_ulid()
            player = "p/48?x=1#@evil.test"
            stub.route(
                "GET",
                "/players/p%2F48%3Fx%3D1%23%40evil.test/gold",
                ps.Reply(200, {"balance": 640}),
            )
            found = await _read(engine, lib, name, Binding(story, name, {"player": player}))
            assert str(found.value) == "640"
            assert len(found.raw_hash) == 64
            (sent,) = stub.received
            assert sent.path == "/players/p%2F48%3Fx%3D1%23%40evil.test/gold"
            assert sent.headers["Authorization"] == f"Bearer {SECRET}"
            assert sent.headers["Host"] == stub.host

            # A redirect is not followed: one request, an adapter error.
            stub.route(
                "GET",
                "/players/moved/gold",
                ps.Reply(302, {}, headers={"Location": "http://10.9.9.9/gold"}),
            )
            with pytest.raises(ProviderUnavailable) as moved:
                await _read(engine, lib, name, Binding(story, name, {"player": "moved"}))
            assert moved.value.reason == "redirect"
            assert [r.path for r in stub.received][-1] == "/players/moved/gold"
            assert len(stub.received) == 2

            # The host is checked again at call time: once the operator no longer allows it,
            # nothing is sent.
            async with ps.allowed_hosts(engine, ["api.bank.test"]):
                with pytest.raises(ProviderUnavailable) as narrowed:
                    await _read(engine, lib, name, Binding(story, name, {"player": player}))
            assert narrowed.value.reason == "host_not_allowed"
            assert len(stub.received) == 2

            # A runtime update (FR-EXT-6): the next resolution reads the new path; a provider
            # resolved before it keeps its registration.
            async with engine.connect() as conn:
                before = await registry.provider_for(
                    conn, name, in_process=lib.in_process_providers
                )
            spec = {k: v for k, v in body.items() if k != "name"}
            spec["manifest"]["values"]["gold"]["read"]["value_path"] = "$.wallet.balance"
            spec["unavailability"] = "unknown"
            updated = await client.put(f"{ADMIN}/{name}", json=spec, headers=WRITE)
            assert updated.status_code == 200, updated.text
            assert updated.json()["config_revision"] == revision0 + 2
            assert updated.json()["unavailability"] == "unknown"
            unchanged = await client.put(f"{ADMIN}/{name}", json=spec, headers=WRITE)
            assert unchanged.json()["config_revision"] == revision0 + 2
            stub.route(
                "GET", "/players/p1/gold", ps.Reply(200, {"balance": 1, "wallet": {"balance": 2}})
            )
            binding = Binding(story, name, {"player": "p1"})
            assert str((await _read(engine, lib, name, binding)).value) == "2"
            assert str((await before.read("gold", binding, None)).value) == "1"
            logged = await _config_rows(engine, name)
            assert [(r.setting, r._mapping["class"], r.config_revision) for r in logged] == [
                (f"external_provider:{name}", "interpretive", revision0 + 1),
                (f"external_provider:{name}.manifest", "interpretive", revision0 + 2),
                (f"external_provider:{name}.unavailability", "interpretive", revision0 + 2),
            ]

            # Deleting a provider a story's State names is refused, listing the story.
            story_id = (await create(client, story_body("Wren", "Ada")))["story_id"]
            await ss.set_level(engine, story_id, "prompt_only")
            await ss.define(engine, story_id, ss.gold(owner="external", provider_id=name))
            in_use = await client.delete(f"{ADMIN}/{name}", headers=WRITE)
            status, code, details = _error(in_use)
            assert (status, code) == (409, "provider_in_use")
            assert details == {"provider": name, "stories": [story_id]}

            # A provider no State names is deleted (204), and is gone.
            other = ps.unique_name("spare")
            made = await client.post(ADMIN, json=ps.in_process_spec(other), headers=WRITE)
            assert made.status_code == 201, made.text
            gone = await client.delete(f"{ADMIN}/{other}", headers=WRITE)
            assert gone.status_code == 204, gone.text
            assert (await client.get(f"{ADMIN}/{other}", headers=READ)).status_code == 404
            assert [r.new for r in await _config_rows(engine, other)][-1] is None


@pytest.mark.part("M6", "FR-EXT-1")
async def test_c_06__bindings(pg_url: str, engine: AsyncEngine, states_delivered: None) -> None:
    """Each story binds its own external ids: two stories bound to one provider with different
    ids read different values (the in-process adapter answers the id back). A binding is
    idempotent, listed and read back; it must give every ``{binding.<key>}`` the manifest uses
    (422 ``binding_incomplete``). Both story creations take ``provider_bindings``; a scenario's
    ``required_providers`` are checked against the registry (422 ``provider_unregistered``)
    and applied live, and a story that kept them inactive gets them live with its first
    binding.

    Complement asserted: a story with an externally owned State and no binding to its provider
    has its turn refused 422 ``provider_unbound`` with nothing recorded; once bound, the turn
    plays."""
    settings = auth_settings(pg_url, model_adapter="stub")
    echo = ps.unique_name("echo")
    async with respond_client(settings, engine) as (client, lib):
        made = await client.post(
            ADMIN, json=ps.in_process_spec(echo, {"handle": "text"}), headers=WRITE
        )
        assert made.status_code == 201, made.text
        fake = ps.ScriptedProvider(echo, echo_key="player").attach(lib)

        # Two stories, one provider, different ids: each reads its own value.
        first = (await create(client, story_body("Wren", "Ada")))["story_id"]
        second = (
            await create(
                client,
                story_body(
                    "Wren", "Ada", provider_bindings={echo: {"external_ids": {"player": "p-2"}}}
                ),
            )
        )["story_id"]
        path = f"/stories/{first}/providers/{echo}/binding"
        bound = await client.put(path, json={"external_ids": {"player": "p-1"}}, headers=WRITE)
        assert bound.status_code == 200, bound.text
        assert bound.json()["external_ids"] == {"player": "p-1"}
        again = await client.put(path, json={"external_ids": {"player": "p-1"}}, headers=WRITE)
        assert again.status_code == 200
        assert again.json()["external_ids"] == {"player": "p-1"}
        listed = (await client.get(f"/stories/{second}/providers", headers=READ)).json()
        assert [(b["provider"], b["external_ids"]) for b in listed["bindings"]] == [
            (echo, {"player": "p-2"})
        ]
        assert (await client.get(path, headers=READ)).json()["external_ids"] == {"player": "p-1"}
        values = []
        for story in (first, second):
            listing = (await client.get(f"/stories/{story}/providers", headers=READ)).json()
            (binding,) = listing["bindings"]
            found = await _read(
                engine, lib, echo, Binding(story, echo, binding["external_ids"]), "handle"
            )
            values.append(found.value)
        assert values == ["p-1", "p-2"]
        assert [r[1] for r in fake.reads] == [first, second]

        # A binding must cover every ``{binding.<key>}`` of the manifest.
        keyed = ps.unique_name("keyed")
        with ps.stub_server() as stub:
            async with ps.allowed_hosts(engine, [stub.host]):
                registered = await client.post(
                    ADMIN, json=ps.http_spec(stub.base_url, keyed), headers=WRITE
                )
                assert registered.status_code == 201, registered.text
        incomplete = await client.put(
            f"/stories/{first}/providers/{keyed}/binding",
            json={"external_ids": {"account": "a-1"}},
            headers=WRITE,
        )
        assert _error(incomplete) == (
            422,
            "binding_incomplete",
            {"provider": keyed, "missing": ["player"]},
        )
        unknown = await client.put(
            f"/stories/{first}/providers/{ps.unique_name('nobody')}/binding",
            json={"external_ids": {}},
            headers=WRITE,
        )
        assert unknown.status_code == 404

        # Creation from a scenario: required providers registered, else refused.
        missing = ps.unique_name("missing")
        scenario, _ = await g.published_scenario(
            client, g.scenario_body(story_settings_defaults={"required_providers": [echo, missing]})
        )
        request = g.from_scenario(scenario, slots={"mother": g.ad_hoc_slot("Mara", 50)})
        status, code, details = await g.refused(client, request)
        assert (status, code, details) == (422, "provider_unregistered", {"providers": [missing]})
        live, _ = await g.published_scenario(
            client, g.scenario_body(story_settings_defaults={"required_providers": [echo]})
        )
        created = await g.create_from(
            client,
            {
                **g.from_scenario(live, slots={"mother": g.ad_hoc_slot("Mara", 50)}),
                "provider_bindings": {echo: {"external_ids": {"player": "p-3"}}},
            },
        )
        story = (await client.get(f"/stories/{created['story_id']}", headers=READ)).json()
        assert story["settings"]["required_providers"] == [echo]
        assert "required_providers" not in (story["settings"].get("inactive") or {})

        # A story created before ``providers`` was delivered kept it inactive: its first binding
        # moves it live, one story configuration change.
        older = (await create(client, story_body("Wren", "Ada")))["story_id"]
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE stories SET settings = settings || CAST(:s AS jsonb) WHERE id = :id"),
                {
                    "s": json.dumps(
                        {
                            "inactive": {
                                "required_providers": {"value": [echo], "capability": "providers"}
                            }
                        }
                    ),
                    "id": older,
                },
            )
        moved = await client.put(
            f"/stories/{older}/providers/{echo}/binding",
            json={"external_ids": {"player": "p-4"}},
            headers=WRITE,
        )
        assert moved.json()["activated_required_providers"] == [echo]
        settings_now = (await client.get(f"/stories/{older}", headers=READ)).json()["settings"]
        assert settings_now["required_providers"] == [echo]
        assert "inactive" not in settings_now
        (change,) = await rows(
            engine,
            "SELECT setting, class, new FROM config_changes WHERE scope = 'story' "
            "AND scope_id = :s AND setting = 'required_providers'",
            s=older,
        )
        assert (change.setting, change._mapping["class"], change.new) == (
            "required_providers",
            "interpretive",
            [echo],
        )

        # The unbound story's turn records nothing; bound, it plays.
        made_story = await create(client, story_body("Wren", "Ada"))
        story_id = made_story["story_id"]
        cast = {c["name"]: c["id"] for c in made_story["characters"]}
        ledger = ps.unique_name("ledger")
        assert (
            await client.post(ADMIN, json=ps.in_process_spec(ledger), headers=WRITE)
        ).status_code == 201
        await ss.set_level(engine, story_id, "prompt_only")
        await ss.define(engine, story_id, ss.gold(owner="external", provider_id=ledger))
        before = await rows(
            engine,
            "SELECT (SELECT count(*) FROM events WHERE story_id = :s) AS events, "
            "(SELECT count(*) FROM operations WHERE story_id = :s) AS operations, "
            "(SELECT timeline_revision FROM stories WHERE id = :s) AS revision",
            s=story_id,
        )
        refused = await client.post(
            f"/stories/{story_id}/respond", json=respond_body(cast["Wren"]), headers=WRITE
        )
        assert _error(refused) == (
            422,
            "provider_unbound",
            {"story_id": story_id, "providers": [ledger]},
        )
        after = await rows(
            engine,
            "SELECT (SELECT count(*) FROM events WHERE story_id = :s) AS events, "
            "(SELECT count(*) FROM operations WHERE story_id = :s) AS operations, "
            "(SELECT timeline_revision FROM stories WHERE id = :s) AS revision",
            s=story_id,
        )
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
        await client.put(
            f"/stories/{story_id}/providers/{ledger}/binding",
            json={"external_ids": {"player": "p-5"}},
            headers=WRITE,
        )
        played = await client.post(
            f"/stories/{story_id}/respond", json=respond_body(cast["Wren"]), headers=WRITE
        )
        assert played.status_code == 200, played.text
        await ss.set_level(engine, story_id, None)


async def _gold_turn(client: Any, stub: Any, story: str, cal: str, narration: str) -> Any:
    stub.reset()
    es.narrator(stub, es.render(beat("narration", narration)))
    ms.renders_for(stub, "Mira", es.render(beat("say", "Mind the edge.")))
    answer = await client.post(
        f"/stories/{story}/respond",
        json=respond_body(cal, None, do="checks his purse"),
        headers=WRITE,
    )
    assert answer.status_code == 200, answer.text
    return answer


@pytest.mark.part("M6", "FR-EXT-2")
@pytest.mark.parametrize("transport", ["in_process", "http"])
async def test_c_06__live_read(
    pg_url: str,
    engine: AsyncEngine,
    states_delivered: None,
    drained_embedding_jobs: None,
    transport: str,
) -> None:
    """Over either adapter: a turn showing an externally owned State reads it live during
    generation (once), and the verify path routes a claim on the value to the pinned version
    (#191's ``state_value`` lookup), never to canon prose and never to a second read: "300" is
    ``supported``, "500" ``contradicted`` and repaired.

    Complement asserted: a planted canon fact that Cal's gold is 500 answers nothing (the 500
    claim stays contradicted, the 300 claim supported)."""
    settings = auth_settings(pg_url, model_adapter="stub")
    name = ps.unique_name("ledger")
    with ps.stub_server() as server:
        server.route("GET", "/players/p-1/gold", ps.Reply(200, {"balance": 300}))
        async with (
            ps.allowed_hosts(engine, [server.host]),
            respond_client(settings, engine) as (client, lib),
        ):
            stub = lib.models.stub
            fake = ps.ScriptedProvider(name)
            if transport == "http":
                await ps.register(engine, ps.http_spec(server.base_url, name))
            else:
                await ps.register(engine, ps.in_process_spec(name))
                fake.attach(lib)
                fake.script("gold", 300)
            story, ids = await structured_story(client, "Cal", "Mira")
            cal = ids["Cal"]
            await ss.set_level(engine, story, "on")
            gold = await ps.external_gold(engine, story, name)

            def reads() -> int:
                if transport == "http":
                    return len([r for r in server.received if r.method == "GET"])
                return len(fake.reads)

            # A planted canon fact with another value.
            await canon.commit(
                engine,
                story,
                sv.proposition(gold.id, cal, sv.number_literal(500)),
                kind=OperationKind.CONTROL,
                provenance="author",
            )
            for said, kind in (("300", "supported"), ("500", "contradicted")):
                before = reads()
                answer = await _gold_turn(
                    client, stub, story, cal, f"Rain taps. Cal has {said} gold left."
                )
                assert reads() == before + 1, said  # one live read per turn, none to verify
                (prompt,) = es.prompts(stub, "NarratorRender")
                assert "300" in prompt
                report = await turn_report(engine, story)
                (verdict,) = [
                    v
                    for v in report["verify"]["verdicts"]
                    if v.get("kind") in ("supported", "contradicted")
                ]
                assert verdict["kind"] == kind, said
                text_out = answer.json()["message"]["text"]
                if kind == "contradicted":
                    assert [r["rung"] for r in report["repairs"]] == [1]
                    assert "500 gold" not in text_out
                else:
                    assert "300 gold left" in text_out
            values = [r.payload["value"] for r in await ps.external_reads(engine, story)]
            assert values == [300, 300]
            await ss.set_level(engine, story, None)
