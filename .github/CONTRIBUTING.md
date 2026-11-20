# Contributing to codex-hpc

Branch from current `master` and open a pull request targeting `master`. Describe the behavior change, link the issue, and include the commands and results used to verify it. Reference the relevant specification when a PR delivers part of its scope. Use a closing reference when a PR satisfies the entire issue.

GitHub's active **Protect master** ruleset applies to maintainers and automation. It requires a PR, an up-to-date branch, linear history, and passing GitHub Actions CI. It blocks force pushes and branch deletion. Reviewer approval is optional. Squash-merge the tested head through GitHub. GitHub deletes the merged feature branch.

The [CI workflow](workflows/ci.yml) runs on every PR to `master` and after merge. Its required job is `tests`. The initial checks validate Actions workflows and local documentation links. Add the executable's CLI tests to this job with the first implementation. When adding independent test jobs, require their exact GitHub Actions check names in the live ruleset before merging the PR. Keep required tests unconditional on PRs. Confirm every applicable test passed on the current head.

## Project skills

- Use [fix-issues](../.agents/skills/fix-issues/SKILL.md) to deliver selected issues through implementation, CI, and squash merge.
- Use [codex-hpc-release](../.agents/skills/codex-hpc-release/SKILL.md) when a maintainer requests a versioned release.
