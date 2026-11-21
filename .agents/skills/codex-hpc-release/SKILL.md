---
name: codex-hpc-release
description: Prepare or resume a requested codex-hpc release through agreed notes, protected PRs, exact-artifact Conda publication to micknudsen, and verified GitHub Releases. Use for an explicit release request; workflow edits and release questions alone do not authorize publication.
---

# Release codex-hpc

Read [Releasing](../../../docs/releasing.md) and the
[contribution policy](../../../.github/CONTRIBUTING.md) before acting. Follow their
artifact, trust, retry and protected-merge rules. Do not copy gwflow's package
dependencies, channel, acceptance status or baseline-refresh procedures.

## Agree on scope and notes

Fetch master and tags. Inspect merged scope, milestone/issue acceptance, version
PRs, drafts, channel publications and retained qualification runs. Resume an
existing attempt rather than creating competing artifacts. Propose a normal
version unless the maintainer explicitly requests a prerelease. A packaging
change or question does not authorize a release.

Draft notes for actual scope, requirements, validation and remaining limits. Save
progress and proposed notes under `.local/releases/<tag>/`. Obtain agreement to
notes before version edits or publication. Agreement authorizes the described
version PR, green squash merge, tag, Conda upload, verified GitHub publication
and final notes PR without another publish prompt. Changed source scope needs
updated notes and renewed agreement.

## Prepare and qualify

Update `setup.cfg` and `conda-recipe/meta.yaml` versions together and archive notes
in `docs/releases/<tag>.md`. If an earlier image exists, pin its verified metadata,
public key and provenance in `release/previous-image.json`; equal inputs retain
its original URL and signing identity. New images require the release signing
secret. Never print or retain private keys; inspect secret names, not values.

Deliver preparation through a protected squash PR. Wait for all five required
checks on the exact merged master commit before tagging it. Verify version/tag
agreement and record the commit. Follow tag-triggered `Release qualification` to
completion. Manual runs and PR disposable images are not publication evidence.
Record the trusted run ID, immutable bundle artifact ID and package digest.

## Publish and finish

Monitor `Publish Conda release`: it uploads the exact qualified package to
`micknudsen`, verifies a fresh channel install and stages a GitHub draft. Do not
rebuild, replace bytes, move tags or bypass branch protection. Follow the documented
resume procedure and retain evidence of partial publication.

Independently download the draft assets and verify asset set, checksums, selected
SIF signature, source/tag and channel package against the qualification bundle.
Reused SIFs stay at their original published URLs. Apply agreed notes and publish
the verified draft, marking prereleases correctly. Then verify the public image
URL embedded in the client, final tag, channel distribution, assets and body.

Synchronize final notes and evidence links through a checked PR; archived notes
must match the GitHub body. Close milestones only when acceptance criteria are
met. Controlled CI peers do not close real-cluster qualification #16. Report
version, release URL, install command, source, qualification/publication evidence
and remaining limits.
