# Releasing Epistrel

Images live at `ghcr.io/nathanpond/epistrel`. Two channels:

| tag | what | when |
|---|---|---|
| `edge`, `sha-<7>` | whatever is on `main` right now; **unstable**, for Console integration and testing | every merge to `main` (`publish-edge.yml`) |
| `X.Y.Z`, `X.Y`, `X`, `latest` | a published release | a `vX.Y.Z` tag (`release.yml`) |

Every publish runs the same gate as a pull request (`ci.yml`) first; nothing is published from a
commit that did not pass it. Images are `linux/amd64` and `linux/arm64`.

## Edge rollback

`edge` always tracks the newest merge. To put an earlier commit back on `edge` (for example when a
merge breaks the Console), re-run the publish for that commit:

```bash
gh workflow run publish-edge.yml -f ref=<full 40-hex sha or branch/tag>
```

The ref must be reachable from `main` (a branch that never merged is refused before anything is
built) and short SHAs are rejected. The run re-runs the gate, rebuilds, and moves `edge` only; the
original `sha-<7>` tag stays addressable. Watch it with `gh run watch`.

## Cutting a release

Versions are `0.x` until the engine is feature-complete; `-rc.N` is the only pre-release form.

1. Open a release PR that bumps `[project].version` in `pyproject.toml` to the new version (nothing
   else on `main` carries a `.devN`). Merge it.
2. Tag the merge commit and push the tag:
   ```bash
   git switch main && git pull --ff-only
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   ```
   (`/n8-release` in the n8SDLC workflow does the same.) Only repository admins can create, move or
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
version already published. Re-running the workflow for an existing tag updates the release's digest
line and leaves the generated notes alone.

## Production rollback

A bad release is undone without rebuilding: `rollback.yml` re-tags the known-good multi-arch image
as `latest` and demotes the bad GitHub release.

```bash
gh workflow run rollback.yml -f good_version=1.2.2 -f bad_version=1.2.3
```

Afterwards:

- `docker buildx imagetools inspect ghcr.io/nathanpond/epistrel:latest` reports `1.2.2`'s digest, and
  so do `1.2` and `1` when they pointed at the bad digest and `1.2.2` belongs to their line.
- The Releases page shows `v1.2.2` badged **Latest** and `v1.2.3` badged **Pre-release** with a
  "Rolled back on <date>; use v1.2.2." line appended to its notes.
- The run's summary lists every tag with its before/after digest and which steps ran.

The run refuses, changing nothing, when the good version's image tag or release does not exist, when
the inputs are not bare `X.Y.Z`, when `good_version == bad_version`, or while a `release.yml` run is
in progress. Deleting the bad release and its tags instead is a manual choice (`gh release delete`,
then remove the image tags in the package settings); demotion keeps the history honest. Anyone who
already pulled `latest` keeps the bad image until they pull again.
