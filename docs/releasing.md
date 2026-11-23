# Releasing codex-hpc

The release flow follows [gwflow](https://github.com/MOMA-AUH/gwflow), using the
`micknudsen` Anaconda channel. It publishes the exact qualified noarch Conda
package, verifies a fresh channel installation, and stages a GitHub draft for
verification and publication with agreed notes. Workflow edits and questions
about releases do not themselves authorize publication.

## Distribution and qualification

The recipe installs a prepared candidate wheel, including its matching helper,
image metadata and public key. Python >=3.9 and OpenSSH are Conda dependencies.
The Linux x86-64 SIF remains a GitHub Release asset: setup downloads it directly
on the login host from the original URL embedded in the package.

Twelve product jobs cover Linux/macOS and Python 3.9 through 3.14. The signed-image
job qualifies new/reused image selection, builds one `.conda` file, installs it
into a fresh prefix and runs all seven remote command suites against copies of
that installation with the real SIF. A final compatibility probe runs pinned
Codex through installed setup/connect. SSH, Slurm and login remain controlled
peers. This does not close [real-cluster qualification #16](https://github.com/micknudsen/codex-hpc/issues/16)
or prove production OAuth, site SSH forwarding or scheduler behavior.

Ordinary PR/master CI uses disposable keys and retains its bundle for three days.
`Release qualification` retains `codex-hpc-conda-release` for 90 days: package,
candidate wheel, image selection, per-suite results, runtime versions, source SHA,
hashes and provenance. Reused images are not duplicated.

## One-time configuration

Configure repository Actions secrets:

* `ANACONDA_API_TOKEN`: an upload token authorized for `micknudsen`. Only the
  publication job receives it.
* `SIF_SIGNING_PRIVATE_KEY`: the PEM private key for new release images. Trusted
  qualification derives its public key and removes temporary key files on exit.
  Never put private keys in Git, logs or artifacts. Ordinary CI uses disposable
  keys; reusing a pinned image does not require the private key.

The publication job uses `GITHUB_TOKEN` with contents-write and actions-read
permissions. Existing protected-master checks remain required.

## Prepare an agreed release

1. Inspect merged scope, outstanding acceptance criteria, tags, drafts and earlier
   publications. Propose a normal version unless a prerelease was explicitly
   requested. Canonical versions are `X.Y.Z`, optionally `aN`, `bN`, `rcN` or `.devN`.
2. Draft notes for actual behavior, requirements, validation and material limits.
   Save progress under ignored `.local/releases/<tag>/`. Agree on the notes before
   release preparation. Agreement authorizes the described version PR, protected
   merge, tag, Conda upload, verified GitHub publication and final notes PR without
   redundant publication approval. Changed source scope needs renewed agreement.
3. Update `setup.cfg` and the literal version in `conda-recipe/meta.yaml` together.
   Keep build number zero for a new version. Archive notes in
   `docs/releases/<tag>.md`. Select the previous image below when available. Use a
   checked PR and squash merge; never bypass master protection.
4. Wait for all five checks on the exact merged commit. Check versions with
   `python scripts/release_identity.py version` and record the full source SHA.
5. Before tagging, confirm the required secret names are configured: the upload
   token always, and the signing key when the image selection requires a build.
   Report missing prerequisites and preserve preparation without pushing the tag.
   Push the matching `v<version>` tag at that commit. `Release qualification`
   checks master ancestry and version agreement, then runs the complete suite
   against that exact source with release image trust.

A manual qualification can test current master or a matching tag: select that
ref and supply its full `source` SHA and expected `tag`. Manual runs never trigger
uploads. Ordinary CI bundles are not trusted tag qualification evidence.

## Select or build the signed image

For the first release, omit `release/previous-image.json`. For later releases,
download and verify the previous release's three public assets against its
recorded evidence, then commit their SHA-256 values in this pin with the version PR:

```json
{
  "tag": "v0.1.0",
  "sha256": {
    "image-release.json": "<64 lowercase hexadecimal characters>",
    "trusted.pem": "<64 lowercase hexadecimal characters>",
    "image-provenance.json": "<64 lowercase hexadecimal characters>"
  }
}
```

`scripts/release_image.py` fetches these exact assets from this repository and
compares resolved image inputs. Equal inputs reuse the original signed bytes,
URL and public key. Changed inputs build and sign a new image before recording
its digest. Both paths run compatibility checks. Do not omit an available pin
merely to rebuild for a client-only change. See [image building](build-image.md)
and [ADR 0003](adr/0003-stop-setup-on-image-download-failure.md).

## Publication and verification

Successful tag qualification triggers `Publish Conda release`. It rechecks the
repository, workflow path, event, exact tag/SHA and master ancestry, selects the
unique unexpired bundle by immutable artifact ID, verifies its manifest and
complete test results, and verifies the SIF signature.

The retained `.conda` bytes upload to `micknudsen` with label `main`; no rebuild
occurs. The workflow installs the exact version/build from the channel into a
fresh prefix, verifies the package hash and all installed client payloads, then
runs setup/connect compatibility with the actual image. Explicit prereleases use
the same channel/label and are marked prerelease on GitHub.

It stages deterministic draft assets and downloads each again for comparison:

* the exact `.conda` package;
* `image-release.json`, `trusted.pem`, `image-provenance.json`;
* `codex-hpc-linux-amd64.sif` only when newly built;
* the versioned validation archive and `SHA256SUMS`.

Publication evidence is retained for 90 days. The workflow leaves GitHub as a
draft. The release skill independently downloads assets, checks their hashes and
the selected image's signature against the trusted bundle, applies agreed notes
and publishes. Mark a normal release as Latest; leave explicit prereleases out
of Latest. A new SIF's public URL becomes available only then: until that
handoff completes, the newly uploaded Conda package cannot complete real setup.

After publication, verify size, digest and signature using the exact public image
URL embedded in the client. Confirm tag, channel package, asset set and notes.
Synchronize evidence links through a final checked PR so `docs/releases/<tag>.md`
matches the GitHub body. Close #16 and its milestone only with separate cluster
acceptance evidence.

## Resume a failed attempt

Record source, tag, qualification run ID, immutable artifact ID, package digest
and failed step. Retry the failed publication run against that retained artifact;
do not rerun qualification and rebuild after uploading. Identical existing
channel distributions and draft assets are accepted after byte verification.
Conflicting bytes, unexpected assets, a published GitHub release or unverifiable
channel state stop publication. Nothing is replaced or clobbered. Only a confirmed
channel 404 permits upload; network and authorization failures are not absence.

Channel indexing gets up to ten fresh solves, 30 seconds apart. Retry an unchanged
publication once for an identified external transient failure; investigate a
repeat. Preserve published versions and tags and use a new version for source
corrections. If the artifact expires, stop rather than silently reconstructing
published bytes. If GitHub publication already succeeded, resume read-only
verification and notes synchronization instead of the draft uploader.
