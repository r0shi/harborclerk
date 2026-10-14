# Releasing

The git tag is the only source of truth for a version. Everything else is
derived from it at build time, and the one place a version is typed by hand —
`pyproject.toml` and `frontend/package.json` — is checked against the tag
before the tag can exist. Nothing in the Xcode projects is edited per release.

## What a build carries

`macos/scripts/build-version.sh` runs `git describe --tags --match 'v[0-9]*'
--long --dirty` on the commit being built and derives (a tag that is not exactly
`vX.Y.Z`, such as `v1.0.0-rc1` or `vendor-x`, fails the build rather than
stamping anything):

| Stamp | Where it lands | Tagged commit, clean | Two commits past the tag | Same, uncommitted changes |
|---|---|---|---|---|
| `MARKETING_VERSION` | `CFBundleShortVersionString`, the About box | `0.9.3` | `0.9.3` | `0.9.3` |
| `CURRENT_PROJECT_VERSION` | `CFBundleVersion`, shown in parentheses | `0` | `2` | `2` |
| `build-hash.txt` | `/api/system/health` `build`, `X-Build-Hash` | `a40dbe9` | `887c3fb` | `887c3fb-dirty` |
| `build-version.txt` | `/api/system/health` `version` | `0.9.3` | `0.9.3+2.g887c3fb` | `0.9.3+2.g887c3fb.dirty` |

`CFBundleVersion` is the number of commits since the tag because Apple allows
only digits and periods there; the dirty marker therefore lives in the hash and
the full version. A bare `0.9.3` in health means exactly one thing: a clean
build of the commit the tag points at. Anything else is a development build and
says so.

`package.sh` passes the first two to both `xcodebuild` invocations, writes the
other two into the venv it bundles, and then reads the built `Info.plist`s back
and fails if they do not carry what it passed. `make dev` passes the same two,
so a Debug build's About box reads `0.9.3 (2)` rather than the project files'
sentinel. A build made from Xcode directly, or from a clone with no `v[0-9]*` tag
reachable, is `0.0.0-dev (0)` or `0.0.0+untagged…` — recognisable, never a
release number.

Without the stamp (Docker, `uv run`), health reports the installed package's
version — `pyproject.toml`'s — as `0.9.3+unstamped`, next to `build: dev`. The
package version is the same for every commit after the bump, so the marker is
what keeps a bare `0.9.3` release-only across deployments.

## Cutting a release

Signing is a human step, so the sequence is split around it.

1. **Bump the sources in a PR.** Set `version` in `pyproject.toml` and
   `frontend/package.json` to `X.Y.Z`, run `uv lock` (the lock records the
   project's own version) and `npm install --package-lock-only` in `frontend/`
   (so does `package-lock.json`). `tests/test_release_version.py` fails CI if
   any of them disagree. Land it on `main`.
2. **Preflight.** On that commit: `bash macos/scripts/release.sh vX.Y.Z --check`.
   It refuses if the three files do not say `X.Y.Z`, if the tree has
   uncommitted changes, or if the tag exists somewhere else.
3. **Tag, then build.** `git tag -a vX.Y.Z -m "Release vX.Y.Z"` — the tag must
   exist *before* the build, or the build describes the previous tag plus a
   distance. Then `cd macos && make all`. `package.sh` prints the `==> Version`
   line; it must read `X.Y.Z (CFBundleShortVersionString X.Y.Z, CFBundleVersion 0, …)`.
4. **Sign by hand.** `make sign`. `notarize.sh` prints both the checkout's
   commit and what the bundle says about itself; if they differ, the bundle is
   stale (#687) — rebuild.
5. **Publish.** `bash macos/scripts/release.sh vX.Y.Z --skip-build`. It
   re-runs the preflight, checks both apps' `Info.plist` and the venv stamp
   carry `X.Y.Z (0)`, pushes the tag, and creates the GitHub release with the
   DMG from `build/output` (the two app zips if no DMG exists). The
   `bump-release-badge` workflow then opens the README badge PR.

Without `--skip-build`, `release.sh vX.Y.Z` does steps 3 and 5 itself with an
unsigned build. A tag it created is deleted again on any exit before the push
(an EXIT trap keyed on the status, so an explicit refusal, a failure inside the
plist check and Ctrl-C during the build all clean up); nothing is pushed until
the bundles carry the version. If the GitHub upload fails *after* the push —
v0.9.2's DMG was over the 2 GiB asset limit — re-run step 5: a tag already on
origin at HEAD with no release is resumed, not refused; one that already has a
release is refused.

## Why the tag and not the files

The files were the source of truth before, and every release up to v0.9.3
shipped with `pyproject.toml` at 0.8.0 and both apps at `1.0 (1)` because
nothing failed when they were not bumped. A tag cannot be forgotten — it is
what makes a release a release — so the derivation starts there and the files
are checked against it rather than trusted. See issue #624.
