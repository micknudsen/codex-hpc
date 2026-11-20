# Expose one workspace per session

Selecting a workspace gives Codex read and write access to that project directory. Its siblings and the rest of the user's remote home stay outside the container. Each session uses a private temporary home.

Before submitting a job, `connect` resolves symlinks in the workspace argument on the login host, displays the resulting absolute path, and validates the directory at that path. The workspace appears at that same path inside the container. Codex starts there so project scripts can use existing absolute paths to files within the workspace.

V1 accepts a single workspace and omits extra project mounts. The workspace argument therefore identifies the full extent of project data available to the session.
