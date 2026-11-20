"""Own one allocation and its cleanup for the duration of connect."""
import re
import shlex
import sys
import time
import uuid


def connect(host, remote, resources):
    marker = "codex-hpc-" + uuid.uuid4().hex
    cancellation = None
    print("Slurm request: nodes=1 tasks=1 " + " ".join(
        name + "=" + (value if value is not None else "Slurm default")
        for name, value in resources.items()), flush=True)
    print("Session job name: " + marker, flush=True)
    print("Find after a lost response: " + shlex.join([
        "ssh", host, shlex.join(["squeue", "--name=" + marker])]), flush=True)
    try:
        if remote("slurm", ["check"]).returncode:
            raise ValueError("required Slurm tools are unavailable; no job was submitted")
        submission = ["sbatch", "--parsable", "--nodes=1", "--ntasks=1",
                      "--job-name=" + marker, "--cpus-per-task=" + resources["cpus"],
                      "--mem=" + resources["mem"], "--time=" + resources["time"],
                      "--output=/dev/null", "--error=/dev/null", "--export=NONE",
                      "--wrap=exec sleep infinity"]
        for name in ("account", "partition"):
            if resources[name] is not None:
                submission.append("--" + name + "=" + resources[name])
        result = remote("slurm", submission)
        response = re.fullmatch(r"([1-9][0-9]*)(?:;([A-Za-z0-9_.-]+))?", result.stdout.strip())
        if response is None:
            raise ValueError("submission failed or its response was lost; use the printed job name "
                             "to inspect squeue before retrying")
        cluster = ["--clusters=" + response.group(2)] if response.group(2) else []
        cancellation = ["scancel", *cluster, "--name=" + marker, response.group(1)]
        job_id = response.group(1)
        print("Cancel: " + shlex.join(["ssh", host, shlex.join(cancellation)]), flush=True)
        if result.returncode:
            raise ValueError("submission transport failed after returning job " + job_id)
        previous = None
        while True:
            result = remote("slurm", ["squeue", *cluster, "--noheader", "--states=all", "--local",
                                      "--jobs=" + job_id, "--format=%i|%j|%T|%R|%N"])
            if result.returncode:
                raise ValueError("cannot query owned Slurm job " + job_id)
            lines = result.stdout.strip().splitlines()
            if not lines:
                raise ValueError("owned Slurm job " + job_id + " disappeared")
            fields = lines[0].split("|", 4)
            if len(lines) != 1 or len(fields) != 5 or fields[:2] != [job_id, marker]:
                raise ValueError("Slurm job identity differs from this session; refusing to use it")
            _, _, state, reason, node = fields
            status = state, reason
            if status != previous:
                print("Job " + job_id + ": " + state + " (" + reason + ")", flush=True)
                previous = status
            if state == "RUNNING":
                print("codex-hpc: allocation ready on " + node +
                      "; session launch is not implemented in this development client", file=sys.stderr)
                return 1
            if state not in ("PENDING", "CONFIGURING"):
                raise ValueError("owned Slurm job stopped in state " + state)
            time.sleep(5)
    except KeyboardInterrupt:
        print("codex-hpc: connection interrupted.", file=sys.stderr)
        return 130
    except (OSError, ValueError) as error:
        print("codex-hpc: " + str(error), file=sys.stderr)
        return 1
    finally:
        if cancellation is not None:
            try:
                result = remote("slurm", cancellation)
                if result.returncode:
                    print("codex-hpc: cleanup failed; run the printed Cancel command.", file=sys.stderr)
            except (OSError, KeyboardInterrupt):
                print("codex-hpc: cleanup interrupted; run the printed Cancel command.", file=sys.stderr)
