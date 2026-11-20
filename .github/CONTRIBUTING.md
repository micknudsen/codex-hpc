# Contributing to codex-hpc

Branch from current `master` and open a pull request targeting `master`. Describe the behavior change, link the issue, and include the commands and results used to verify it. Reference the relevant specification when a PR delivers part of its scope. Use a closing reference when a PR satisfies the entire issue.

GitHub's active **Protect master** ruleset applies to maintainers and automation. It requires a PR, squash merges, and linear history. It blocks force pushes and branch deletion. Reviewer approval is optional. Update the branch with current `master` before merging. GitHub deletes the merged feature branch.

Introduce GitHub Actions in the first PR that implements testable product behavior. Run the product tests on PRs to `master` and after merge. Before merging that PR, add the observed GitHub Actions test check names to **Protect master** and enable the up-to-date branch requirement. Require each subsequent test job in the same way. All tests must pass on the current PR head.

## Project skills

- Use [fix-issues](../.agents/skills/fix-issues/SKILL.md) to deliver selected issues through implementation, CI, and squash merge.
- Use [codex-hpc-release](../.agents/skills/codex-hpc-release/SKILL.md) when a maintainer requests a versioned release.
