# codex-hpc

The first release targets interactive Codex sessions in a single workspace on a Slurm cluster. The [v0.1.0 milestone](https://github.com/micknudsen/codex-hpc/milestone/1) tracks the [MVP specification](https://github.com/micknudsen/codex-hpc/issues/1).

See [Contributing](.github/CONTRIBUTING.md) for the protected PR workflow and the project skills for implementing issues and preparing releases.

Maintainers can [build and qualify a signed image candidate](docs/build-image.md) with recorded inputs and a matching client/helper package. Candidate creation does not publish a release.

## Development client

A candidate client wheel bundles its matching helper, image metadata and trust key.
Its `setup` prepares that release on the login host and reports success only after
verification and atomic activation. A source-only install has no release bundle;
it reports discovered paths and exits nonzero. `connect` validates its workspace
and installed release, submits and monitors one owned allocation, then releases
it and exits nonzero because compute-node session launch is still being implemented.
Setup never submits a job; neither command starts sign-in at this stage.

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
holds the allocation with `sleep infinity`; the future interactive `srun` step
will run in this same allocation. This development version releases the ready
allocation immediately and reports that session launch is not implemented.

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

## Remote setup

Install a qualified candidate wheel, then run `codex-hpc setup --host my-cluster`.
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
and controlled scheduler executables. These
do not claim real-cluster or real-SSH qualification. CI runs the same suite
on Ubuntu 24.04 and macOS with Python 3.9 and 3.14 for PRs and after merge to `master`.
