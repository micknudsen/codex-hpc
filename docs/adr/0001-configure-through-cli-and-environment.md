# Configure each invocation through command-line arguments and environment variables

Each command takes its settings directly from command-line arguments, with corresponding `CODEX_HPC_*` environment variables supplying defaults. An explicit argument takes precedence. Users can run a command with its settings visible at the point of use and use the environment for values they reuse. This removes the named profile and its configuration schema from the public interface.

`--host` selects the login host and defaults to `CODEX_HPC_HOST`. It accepts an SSH destination such as `user@login.example.org` or an alias from the user's SSH configuration. SSH resolves the destination using its normal configuration.

The installed helper, image cache, and persistent login state each have an independent remote root. A user can put a large image cache on shared storage while keeping credentials in a private state directory.

| Contents | CLI override | Environment override | Remote default | Remote fallback |
| --- | --- | --- | --- | --- |
| Helper | `--data-root` | `CODEX_HPC_DATA_ROOT` | `$XDG_DATA_HOME/codex-hpc` | `~/.local/share/codex-hpc` |
| Image cache | `--cache-root` | `CODEX_HPC_CACHE_ROOT` | `$XDG_CACHE_HOME/codex-hpc` | `~/.cache/codex-hpc` |
| Login state | `--state-root` | `CODEX_HPC_STATE_ROOT` | `$XDG_STATE_HOME/codex-hpc` | `~/.local/state/codex-hpc` |

For each root, the client uses the CLI override first, then the corresponding `CODEX_HPC_*` value. With neither override, it uses the login host's XDG variable if that variable contains an absolute path. Otherwise it resolves the fallback against the remote user's home directory.
