# codex-hpc

The first release targets interactive Codex sessions in a single workspace on a Slurm cluster. The [v0.1.0 milestone](https://github.com/micknudsen/codex-hpc/milestone/1) tracks the [MVP specification](https://github.com/micknudsen/codex-hpc/issues/1).

See [Contributing](.github/CONTRIBUTING.md) for the protected PR workflow and the project skills for implementing issues and preparing releases.

Maintainers can [build and qualify a signed image candidate](docs/build-image.md) with recorded inputs and a matching client/helper package. Candidate creation does not publish a release.

## Installation

Once the first release is published, install from the `micknudsen` Conda channel:

```sh
conda create -n codex-hpc --override-channels -c micknudsen -c conda-forge codex-hpc
conda activate codex-hpc
codex-hpc setup --host my-cluster
codex-hpc connect '~/projects/example' --host my-cluster
```

The package bundles its matching helper, image metadata and public key, and
depends on OpenSSH. The signed SIF is downloaded on the login host from GitHub
Releases. See [Releasing](docs/releasing.md) for qualification and publication.

## Development client

A candidate client wheel bundles its matching helper, image metadata and trust key.
Its `setup` prepares that release on the login host and reports success only after
verification and atomic activation. A source-only install has no release bundle;
it reports discovered paths and exits nonzero. `connect` validates its workspace
and installed release, submits and monitors one owned allocation, and launches
interactive Codex on its assigned node. Setup never submits a job. Managed
credentials are shared within the selected state root. First sign-in uses a local
browser callback forwarded to the allocated node; device-code sign-in is opt-in.

Install from this checkout with Python 3.9 or later:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/codex-hpc --help
.venv/bin/codex-hpc setup --host my-cluster
.venv/bin/codex-hpc connect '~/projects/example' --host user@login.example.org
```

The client needs OpenSSH on `PATH`. The login host needs `python3` and a POSIX
shell; setup also needs `curl` and Apptainer with PEM verification support. SSH uses your normal configuration, authentication and host-key checks;
agent and X11 forwarding are disabled. SSH aliases work without another profile
file. Discovery reports SSH, remote interpreter and storage errors to the caller.

Each command accepts these settings. An explicit CLI value overrides the local
environment value; otherwise the remote default is used. `--host` has no default.

| CLI option | Local environment | Remote default when unset |
| --- | --- | --- |
| `--host` | `CODEX_HPC_HOST` | Required SSH destination |
| `--data-root` | `CODEX_HPC_DATA_ROOT` | `$XDG_DATA_HOME/codex-hpc` or `~/.local/share/codex-hpc` |
| `--cache-root` | `CODEX_HPC_CACHE_ROOT` | `$XDG_CACHE_HOME/codex-hpc` or `~/.cache/codex-hpc` |
| `--state-root` | `CODEX_HPC_STATE_ROOT` | `$XDG_STATE_HOME/codex-hpc` or `~/.local/state/codex-hpc` |

XDG defaults come from the **login host**, independently for each root. An unset,
empty or relative XDG home uses the fallback. Override paths must be absolute
POSIX paths or begin with `~/`; quote paths so your local shell does not expand
`~`, `$` or shell expressions. Only that leading `~/` is expanded, using remote
`HOME`. Spaces, Unicode and shell metacharacters in paths stay literal. Bare
relative paths, `~otheruser` paths, control characters, files and paths beneath
non-directory ancestors produce errors. Roots may be directories that do not yet
exist. Setup checks writability and available space before an image transfer.
Filesystem quotas can still reject a write; setup reports that failure without
changing the active installation.

`connect` requires an explicit existing workspace, given as an absolute path or
quoted `'~/project'`. It resolves project symlinks on the login host and displays
the canonical path. Missing directories, regular files, bare relative paths, and
workspaces that expose managed helpers, credentials, images or temporary files
are rejected before requesting resources. This includes managed paths exposed
through workspace symlinks. Run either command with `--help` for its arguments.

Every connection checks the matching installed helper, metadata and public key,
then rechecks the image's signed digest and signature. A missing or incomplete
release prints the exact `setup` command with the selected destination and roots.
After preflight, connect checks for `sbatch`, `squeue`, and `scancel` on the login
host before requesting an allocation.

## Upgrades and storage reclamation

After updating the local client, rerun `setup`. A successful activation immediately
collects obsolete managed helpers and images that are no longer needed. The active
installation keeps its image even with no sessions. Images are shared by architecture
and signed digest, including installations with different data roots using one cache.
Unrelated files and the working installation after a failed upgrade are preserved.

Connect holds a remote release lease before preflight completes, through queueing,
sign-in and execution. An upgrade can proceed during that time; each session keeps
its selected helper and image. The last session's exit collects obsolete files, and
later setup retries cleanup missed after interruption. No separate collection command
or local session journal is required.

If the lease connection disappears, cleanup inspects its exact scheduler marker.
Running or queued jobs retain their files; unavailable or malformed ownership evidence
defers cleanup. A submission whose response was never acknowledged and whose marker
has never been observed is retained conservatively, without a timeout. Use the printed
scheduler recovery information to resolve the job; subsequent setup can collect once
an observed job is absent. Cleanup failures report that setup can be rerun to retry.

## Allocation and recovery

Connect requests one node and one task and prints the resolved resources before
submission. CLI options override the corresponding local environment values.
Account and partition are left to Slurm when omitted and are displayed as such.

| CLI option | Local environment | Default |
| --- | --- | --- |
| `--cpus` | `CODEX_HPC_CPUS` | `2` |
| `--mem` | `CODEX_HPC_MEM` | `4G` |
| `--time` | `CODEX_HPC_TIME` | `02:00:00` |
| `--account` | `CODEX_HPC_ACCOUNT` | Slurm choice |
| `--partition` | `CODEX_HPC_PARTITION` | Slurm choice |

The client prints a unique job name before one `sbatch --parsable` submission and
an exact SSH `scancel` command as soon as it receives the job ID. The batch job
holds the allocation with `sleep infinity`; the interactive `srun` step runs in
this same allocation, with one node/task and the requested CPUs.

While pending, connect polls no more often than every five seconds and prints
only changes in state or reason. There is no client queue timeout; Slurm's time
limit applies after allocation, not to queue wait. Ctrl-C attempts immediate
cleanup when the ID is known. Cleanup always combines the exact ID with the
unique job name, including after startup errors or an ordinary return. It never
cancels by user, partition, or job name alone. Ambient `SBATCH_*`, `SQUEUE_*`,
`SCANCEL_*`, and `SLURM_CLUSTERS` options are excluded from managed commands;
site configuration and authentication settings remain available.

If the submission response is lost, connect does not resubmit or guess an ID.
Use its printed `squeue --name=...` command to find the job and cancel the confirmed
ID on the cluster. If automatic cleanup fails, use the printed cancellation command.
Job IDs stay in memory; no local session journal is written.

These command contracts follow the official [sbatch](https://slurm.schedmd.com/sbatch.html),
[squeue](https://slurm.schedmd.com/squeue.html), and [scancel](https://slurm.schedmd.com/scancel.html)
documentation. A cluster-qualified submission response retains that cluster for
polling and cancellation.

## Compute session

Once the owned job is running, the client uses the selected login host as an SSH
jump host to its assigned node, with the login account's remote username. Normal
host-key verification and authentication remain enabled; agent/X11 forwarding and
connection sharing are disabled for the session connection. SSH and `srun --pty`
carry the interactive terminal, and the Codex exit status is returned to the client.

The node must be Linux x86-64, provide compatible Apptainer and Slurm tools, and
access the workspace, helper, image and state directory at their expected shared
paths. Image/helper bytes are checked before launch. A failure identifies the
platform, path or capability and triggers owned-job cleanup.

The step creates a private node-local directory under `/tmp`, independent of a
shared `TMPDIR`. Its home and scratch expire when the step exits, including Ctrl-C.
Apptainer disables automatic host filesystem, configured bind, home and current
directory mounts, ignores ambient container mount/variable overrides, and mounts
one writable workspace at its original absolute path. Codex starts there. The
container home is `/codex-hpc-home`; a workspace overlapping that reserved path is
rejected. Workspace edits persist. SSH loss and startup failure return through the
same allocation cleanup path; hard termination still requires the printed manual
recovery command.

The launch follows [Slurm srun](https://slurm.schedmd.com/srun.html) and
[Apptainer bind isolation](https://apptainer.org/docs/user/1.5/bind_paths_and_mounts.html).
Actual site forwarding, scheduler limits and authentication are qualified separately.

## Shared login

Sessions using the same remote state root share only the Codex login. The helper
keeps its credential document and lock in a private `credentials/` directory
(mode 0700, files 0600). It stages `auth.json` into the session's private home,
sets `CODEX_HOME` there, and explicitly selects Codex's file credential store.
The persistent directory is never mounted into the container. An empty state root
supports a first login; a separate `--state-root` keeps another account independent.
SSH aliases and workspaces do not change which login a state root selects.

Credential reads and commits hold a short lock, allowing overlapping sessions.
An updated document replaces the saved login atomically only if the saved document
has not changed since that session started. Closing a stale session therefore
preserves a newer saved login. Interrupted commits leave a complete old or new
document. Malformed or symlinked documents are rejected without logging their
contents. History, personal settings and login logs in the temporary home expire;
only `auth.json` is carried forward. Workspace edits remain in the workspace.

This follows the pinned Codex 0.170.0
[auth document](https://github.com/openai/codex/blob/rust-v0.170.0/codex-rs/login/src/auth/storage.rs)
and [file-store setting](https://github.com/openai/codex/blob/rust-v0.170.0/codex-rs/config/src/types.rs).
Tests use disposable credential documents and a controlled Codex process; they
do not authenticate a production account.

## First sign-in

When the state root has no saved login, browser mode checks local port 1455 before
submitting a job. A conflict identifies the port and asks you to free it and rerun
`connect`. A cached login does not need the port. Once allocated, a short sign-in
step checks Codex's login status on the compute node. The login host only verifies
the image; it does not run Codex.

When sign-in is needed, the client confirms an owned SSH callback tunnel through
the same jump host before opening your local browser. If browser launch fails,
open the printed URL on the same computer; the tunnel and allocation remain open.
The tunnel closes immediately after sign-in, before the interactive step starts,
and on failure or cancellation. Both steps use the same allocation. The saved
login is committed using the same concurrent-session rules described above; the
interactive step reads the latest saved login. No tokens or login codes are kept
in client logs, and node login logs stay in the private temporary home.

Use `codex-hpc connect '~/project' --host my-cluster --device-auth` to explicitly
select device-code sign-in. There is no automatic fallback to device codes.
Device mode prints Codex's URL/code and does not create a callback tunnel.

Pinned Codex 0.170.0 uses port 1455, with 1457 as its node-side fallback. The client
forwards the callback port from the emitted sign-in URL; a newly busy local port
after queueing stops startup and releases the owned job. This follows the pinned
[login commands](https://github.com/openai/codex/blob/rust-v0.170.0/codex-rs/cli/src/main.rs)
and [callback server](https://github.com/openai/codex/blob/rust-v0.170.0/codex-rs/login/src/server.rs).
Actual OAuth and site SSH forwarding are still part of real-cluster qualification.

## Remote setup

Install a released Conda package, then run `codex-hpc setup --host my-cluster`.
Setup downloads a missing image directly on the login host from its bundled
original GitHub Release URL. The cache uses architecture and the SHA-256 of the
signed bytes, so newer clients can share an older image without downloading it
again. Every setup checks size, SHA-256 and the signature using its dedicated
public key, including cache hits. It does not import keys into your global
Apptainer configuration.

Setup locks canonical data and cache roots while staging and activating the
release. Concurrent attempts share the verified download. Transfer failures,
quota errors and interrupted staging preserve the previous `current` activation;
rerun the same command to retry. A damaged cached image or installed release is
reported rather than activated. Unrelated files and saved credentials are left
alone. Obsolete-release collection is implemented separately.

## Product tests

```sh
python3 -m pip install setuptools wheel
python3 -m unittest discover -s tests -v
```

The suite builds a wheel without fetching dependencies, installs it into a fresh
virtual environment, and invokes the installed `codex-hpc` entry point outside
the checkout. A controlled SSH executable runs the packaged discovery script in
a separate process with its own remote `HOME` and XDG environment. Tests cover
argument errors, independent precedence, literal paths and observable failures.
Setup scenarios also cover cold/warm installation, low space, transfer and quota
errors, wrong keys, damaged bytes, concurrent attempts, interrupted transfer and
retry, and a newer client referencing an older asset. The regular suite controls
Apptainer responses; the signed-image CI job repeats setup with the actual signed
SIF and real signature verification. Connect tests exercise canonical paths, managed-path isolation,
literal setup recovery, missing or changed installed files and cached-image
tampering, with zero Slurm submissions on preflight failure. Allocation tests cover
resource precedence, pending/running/failed/vanished jobs, actual five-second poll
spacing, deduplicated status, lost responses, Ctrl-C before/after ID receipt, and
cleanup with unrelated or reused job IDs present. CI repeats these with the real SIF
and controlled scheduler executables. Compute tests cover jump-host/step identity,
TTY input, exit propagation, node/platform/runtime errors and interrupted process
and scratch cleanup. Linux image CI also runs a probe inside the real container
through packaged connect: workspace writes persist while sibling, personal and
credential files stay inaccessible. The probe substitutes a controlled process for
interactive Codex, whose pinned executable is qualified during image building. These
do not claim real-cluster or real-SSH qualification. CI runs the same suite
on Ubuntu 24.04 and macOS with Python 3.9 and 3.14 for PRs and after merge to `master`.

Credential scenarios exercise first login, cached reuse with a busy callback port,
separate roots, shared aliases, overlapping refresh and stale-close ordering,
interrupted atomic persistence, private permissions and malformed/symlink rejection.
Linux CI also reads and updates the private auth copy inside the real SIF while
checking that the saved credential store stays inaccessible.

Sign-in scenarios use disposable login/browser/SSH peers and real loopback binds.
They cover prequeue and late port conflicts, confirmed-tunnel ordering, manual URL
fallback, cached and explicit device flows, failed sign-in, cancellation, unrelated
connection survival, and a newer credential saved during authentication. CI repeats
these through packaged connect with real signed-image verification.
