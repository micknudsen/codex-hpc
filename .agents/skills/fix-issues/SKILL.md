---
name: fix-issues
description: Deliver selected codex-hpc issues or a queue of ready-for-agent issues through implementation, CI repair, and protected squash merge. Activate for an issue execution or resume request.
---

# Fix issues

1. Read the [contribution policy](../../../.github/CONTRIBUTING.md), [glossary](../../../GLOSSARY.md), relevant [decisions](../../../docs/adr/), and the issue bodies, comments, dependencies, and linked PRs in `micknudsen/codex-hpc`. Honor requested issue IDs, milestone, order, and limits. For a general queue, snapshot open issues labeled `ready-for-agent` and order by dependencies, then issue number. Keep the requested scope when resuming.
2. Work one issue at a time. Fetch current `master`, preserve unrelated work, and reuse a matching branch or PR when resuming. Create a feature branch before changing files. Use the maintainer's `chief-of-staff` skill for coordinating a queue and `implement` for issue delivery when available. Use the issue and its linked specification to identify acceptance criteria and the agreed test boundary. Exercise the affected behavior through that boundary.
3. Review the diff against the issue, run the applicable tests, commit, and push the feature branch. Open a PR with concrete test evidence and a closing reference for the issue it fully resolves. Reference a broader spec without closing it when delivering only part of it.
4. Monitor all CI on the current head. Diagnose failures from job logs, repair them on the branch, and rerun checks. Update with current `master` when required. Verify the effective protection and passing tests, then squash-merge with `gh pr merge NUMBER --squash --match-head-commit SHA` using the verified head. Preserve the ruleset throughout.
5. Check that GitHub reports a merged PR, that the issue is resolved, and that the merge commit passes CI. Repair post-merge regressions through a new PR. Continue with the next independent issue. Report issue and PR links, checks, and concrete blockers. A request to configure the workflow or plan a milestone does not start delivery.
