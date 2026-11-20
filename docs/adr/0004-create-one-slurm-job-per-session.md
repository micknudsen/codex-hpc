# Create one Slurm job per session

In v1, `connect` submits a new Slurm job for each session. Attaching to an existing job with `--job-id` is outside MVP scope. This gives every session the same cleanup rule. It cancels only the job it created.

Every v1 session runs one interactive Codex process in a Slurm allocation of exactly one node and one task. Users can adjust CPUs, memory, and runtime. These default to two CPUs, `4G` of memory, and two hours. Before submission, `connect` displays the resolved request and identifies any account or partition left to Slurm's defaults.

While the job is pending, `connect` waits and displays changes in job state or pending reason. Slurm controls the queue wait. V1 omits `--wait-timeout`. Pressing Ctrl-C while waiting immediately requests cancellation of that session's job. The runtime limit begins when the allocation starts.

`connect` prints the unique job name before submission and the exact `scancel` command as soon as Slurm returns the job ID. It attempts cleanup on exit or failure. If the submission response is lost, the user can find the job by its printed name in `squeue` on the cluster and cancel it by ID. Slurm's commands cover manual recovery, so v1 omits `sessions list` and `cancel SESSION_ID`.

The client keeps the job ID in memory for cleanup. V1 does not write a local session journal. If the client and its terminal output are both lost, the user can list their jobs on the cluster with `squeue`.

Once Slurm assigns a node, `connect` uses the selected login host (`--host` or `CODEX_HPC_HOST`) as the SSH jump host to reach that node. This requires the login host to allow SSH forwarding to the assigned node. Slurm supplies the node name.

The workspace, installed helper, image, and persistent login directory must be accessible at the same paths on the login host and the assigned node. A first login can populate an empty login directory. The session's temporary home is local to the assigned node.

Before starting Codex, `connect` checks image compatibility and access to the required paths on the assigned node. If a path is unavailable, it identifies the path and releases the job. These checks run during ordinary startup, so v1 omits a separate compute diagnostic command.

V1 requires a Linux x86-64 compute node. On an unsupported node, the error reports the detected platform and the required platform.
