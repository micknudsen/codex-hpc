# Share a Codex login within a remote state directory

Sessions that use the same remote state directory reuse its Codex login across workspaces and SSH aliases. A user can select a separate directory with `--state-root` or its default, `CODEX_HPC_STATE_ROOT`, to keep a second Codex account's credentials separate.

`setup` can finish before the user signs in to Codex. The first `connect` guides the user through sign-in on the allocated compute node if the selected state directory has no valid login. Later sessions reuse the saved login while it remains valid.

When sign-in is needed, the default flow forwards the local browser's callback over SSH to Codex on the allocated node. After the tunnel is ready, the client automatically opens the sign-in URL in the user's local browser. Device-code sign-in is optional and requires explicit selection.

If the browser fails to open, `connect` prints the sign-in URL and keeps the callback tunnel open so the user can finish sign-in manually on the same computer, within the current allocation.

Only browser sign-in needs the local callback port. `connect` closes the tunnel as soon as sign-in finishes. A session using a valid saved login can start even when another application occupies the port.

When `connect` knows browser sign-in is needed before submitting the job, it checks the local callback port first. If the port is busy, it exits before submission, identifies the port, and tells the user to free it and rerun `connect`.

Sessions in different workspaces can use the shared login concurrently. If one session updates the saved credentials, closing another session with an older copy must preserve the update.

The Codex login is the only state carried forward from the session's temporary home. Conversation history and personal settings stored there expire with the session. Files written in the workspace persist across sessions.
