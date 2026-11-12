# Releasing Epistrel

Images live at `ghcr.io/nathanpond/epistrel`. Two channels:

| tag | what | when |
|---|---|---|
| `edge`, `sha-<7>` | whatever is on `main` right now; **unstable**, for Console integration and testing | every merge to `main` (`publish-edge.yml`) |
| `X.Y.Z`, `X.Y`, `X`, `latest` | a published release | a `vX.Y.Z` tag (`release.yml`) |

Every publish runs the same gate as a pull request (`ci.yml`) first, then the deployment smoke
(C-10): a `smoke` job builds the amd64 image, starts it with `docker-compose.yml` and
`tests/smoke/compose.override.yml`, and runs `pytest -m smoke` against it with
`tests/smoke/fake_openai.py` as the one model endpoint. Nothing is published from a commit that did
not pass both; a red smoke uploads the compose logs as the `smoke-compose-logs` artifact. Images are
`linux/amd64` and `linux/arm64`.

## Edge rollback

`edge` always tracks the newest merge. To put an earlier commit back on `edge` (for example when a
merge breaks the Console), re-run the publish for that commit:

```bash
gh workflow run publish-edge.yml -f ref=<full 40-hex sha or branch/tag>
```

The ref must be reachable from `main` (a branch that never merged is refused before anything is
built) and short SHAs are rejected. The run re-runs the gate, rebuilds, and moves `edge` only; the
original `sha-<7>` tag stays addressable. The smoke it runs is current `main`'s suite against the
image built from the old ref. Watch it with `gh run watch`.

## Cutting a release

Versions are `0.x` until the engine is feature-complete; `-rc.N` is the only pre-release form.

1. Open a release PR that bumps `[project].version` in `pyproject.toml` to the new version (nothing
   else on `main` carries a `.devN`). Merge it.
2. Tag the merge commit and push the tag:
   ```bash
   git switch main && git pull --ff-only
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   ```
   (`/n8-release` in the n8SDLC workflow does the same.) Push release tags one at a time, and wait
   for each tag's `release.yml` run to start before pushing the next: GitHub keeps one pending run
   per concurrency group (see Production rollback). Only repository admins can create, move or
   delete `v*` tags — the `release-tags` tag ruleset (`.github/rulesets/release-tags.json`, created
   with `gh api -X POST repos/nathanpond/Epistrel/rulesets --input .github/rulesets/release-tags.json`).
3. `release.yml` validates the tag (must match `pyproject.toml`'s version, must be on `main`, an rc of
   an already-released stable version is refused), runs the gate, pushes the image and creates the
   GitHub release with notes generated from merged PR titles (categories in `.github/release.yml`),
   ending with the image reference and digest.

What results:

| tag pushed | image tags | GitHub release |
|---|---|---|
| `v1.2.3`, highest stable so far | `1.2.3`, `1.2`, `1`, `latest` | marked **Latest** |
| `v1.2.3` while `1.3.0` exists | `1.2.3`, `1.2` (if highest in 1.2.x) | not marked latest |
| `v1.2.3-rc.1` | `1.2.3-rc.1` only | marked **Pre-release** |

`latest` is never moved by a pre-release, and never by a version lower than the highest stable
version already published. A release a rollback demoted (a stable version marked pre-release) does
not count as higher.

Re-running the workflow for a tag that is already released runs the gate and the smoke again, but
builds, pushes and re-tags nothing: the `rerun` job reads the digest back from the `X.Y.Z` image tag
and rewrites the release's `Image:` line with it, so the recorded digest never changes after release.
The generated notes are left alone. It also re-asserts the GitHub flags: an `-rc.N` stays
**Pre-release**, and the highest stable version is marked **Latest** (a lower one's Latest is left
alone). A re-run never undoes a rollback: a release a rollback demoted keeps its flags, and
re-promotion (below) is the way back.

## Production rollback

A bad release is undone without rebuilding: `rollback.yml` re-tags the known-good multi-arch image
as `latest` and demotes the bad GitHub release. A floating tag already on the good digest is reported
"unchanged" and not re-created.

```bash
gh workflow run rollback.yml -f good_version=1.2.2 -f bad_version=1.2.3
```

Afterwards:

- `docker buildx imagetools inspect ghcr.io/nathanpond/epistrel:latest` reports `1.2.2`'s digest, and
  so do `1.2` and `1` when they pointed at the bad digest and `1.2.2` belongs to their line.
- The Releases page shows `v1.2.2` badged **Latest** and `v1.2.3` badged **Pre-release** with a
  "Rolled back on <date>; use v1.2.2." line appended to its notes.
- The run's summary and its job log list every tag with its before/after digest, the promote and
  demote lines, and the post-condition line.

### Re-promotion

Run it with `good_version` alone to put a release back where it belongs, for example once a
demoted release has been fixed or a rollback went too far:

```bash
gh workflow run rollback.yml -f good_version=0.1.1
```

`python -m tools.release retag` picks every floating tag the good version is the highest of
(`latest`, `X.Y`, `X`), counting the good version even while it is demoted and ignoring every other
demoted release. Those tags are re-pointed at the good version's recorded digest, with no rebuild, and
the release loses its Pre-release badge. It is marked **Latest** only when `latest` is among them;
otherwise it is marked not latest. For example, re-promoting `0.1.1` re-points `latest`, `0.1` and `0`
when `0.1.0` is the only other release, only `0.1` when `0.2.0` exists, and `0.1` and `0` when
`1.0.0` exists. Leave `bad_version` empty to re-promote: any version passed there is demoted.

The run refuses, changing nothing, when the good version's image tag or release does not exist, when
the inputs are not bare `X.Y.Z`, or when `good_version == bad_version`.

`rollback.yml` and `release.yml` share the concurrency group `publish-release`, which never cancels
a run in progress. A rollback dispatched while a release runs waits for the release to finish, and a
release waits for a running rollback. GitHub keeps at most one *pending* run per group, though: a
third run queued behind a pending one cancels the pending one. So dispatch a rollback only when no
release run is waiting to start (the Actions tab shows it as queued or pending), or the rollback
cancels that release; and push no tag while a rollback is waiting.

Deleting the bad release and its tags instead is a manual choice (`gh release delete`, then remove
the image tags in the package settings); demotion keeps the history honest. Anyone who already
pulled `latest` keeps the bad image until they pull again.
