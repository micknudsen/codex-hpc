"""Owned Slurm lifecycle through the installed connect command."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time

from test_setup import RemoteCLI


class AllocationFixture(RemoteCLI):
    def setUp(self):
        super().setUp()
        self.workspace = self.home / "workspace æ"
        self.workspace.mkdir()
        self.env["SCHEDULER_LOG"] = str(self.root / "scheduler.jsonl")
        self.env["JOBS"] = str(self.root / "jobs.json")
        self.env["STATES"] = json.dumps([["RUNNING", "None"]])
        Path(self.env["JOBS"]).write_text(json.dumps({"777": "unrelated-job"}))
        script = '''
import json, os, sys, time
from pathlib import Path
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
def option(name):
    return next((arg.split("=", 1)[1] for arg in args if arg.startswith(name + "=")), None)
jobs_path = Path(os.environ["JOBS"])
jobs = json.loads(jobs_path.read_text())
record = {"tool": tool, "args": args, "time": time.time(),
          "ambient": {key: value for key, value in os.environ.items()
                      if key.startswith(("SBATCH_", "SQUEUE_", "SCANCEL_"))}}
if tool == "sbatch":
    jobs["321"] = option("--job-name")
    jobs_path.write_text(json.dumps(jobs))
    with open(os.environ["SCHEDULER_LOG"], "a") as log: log.write(json.dumps(record) + "\\n")
    if os.environ.get("SUBMIT_DELAY"):
        try: time.sleep(30)
        except KeyboardInterrupt: raise SystemExit(130)
    if os.environ.get("LOST_RESPONSE"):
        raise SystemExit(255)
    print(os.environ.get("JOB_RESPONSE", "321"), flush=True)
    raise SystemExit(int(os.environ.get("SUBMIT_EXIT", "0")))
elif tool == "squeue":
    log = Path(os.environ["SCHEDULER_LOG"])
    polls = sum(json.loads(line)["tool"] == "squeue" for line in log.read_text().splitlines())
    states = json.loads(os.environ["STATES"])
    state, reason = states[min(polls, len(states) - 1)]
    if state == "REUSED":
        jobs["321"] = "unrelated-replacement"
        jobs_path.write_text(json.dumps(jobs))
    if state == "VANISHED":
        jobs.pop("321", None)
        jobs_path.write_text(json.dumps(jobs))
    if state == "QUERY_ERROR":
        with open(os.environ["SCHEDULER_LOG"], "a") as log: log.write(json.dumps(record) + "\\n")
        raise SystemExit(1)
    if "321" in jobs:
        print("321|" + jobs["321"] + "|" + state + "|" + reason + "|node01")
elif tool == "scancel":
    job_id = args[-1]
    if jobs.get(job_id) == option("--name"):
        del jobs[job_id]
    jobs_path.write_text(json.dumps(jobs))
with open(os.environ["SCHEDULER_LOG"], "a") as log:
    log.write(json.dumps(record) + "\\n")
'''
        for tool in ("sbatch", "squeue", "scancel"):
            self.script(tool, script)
        self.assertEqual(self.setup().returncode, 0)

    def command(self):
        return [str(self.cli), "setup", "--host", "test-login"]

    def connect_command(self, *arguments):
        return [str(self.cli), "connect", str(self.workspace), "--host", "test-login", *arguments]

    def connect(self, *arguments):
        return subprocess.run(self.connect_command(*arguments), env=self.env, cwd=self.root,
                              capture_output=True, text=True, timeout=45)

    def scheduler_events(self):
        path = Path(self.env["SCHEDULER_LOG"])
        events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        # These assertions cover allocation polling/submission/cancellation. The
        # release collector additionally makes read-only marker inspections.
        return [event for event in events if not (event["tool"] == "squeue" and any(
            arg.startswith("--name=") for arg in event["args"]))]


class AllocationCLI(AllocationFixture):
    def test_default_request_prints_recovery_and_cleans_only_owned_job(self):
        result = self.connect()
        events = self.scheduler_events()
        self.assertEqual([event["tool"] for event in events], ["sbatch", "squeue", "scancel"])
        submitted = events[0]["args"]
        for flag in ("--nodes=1", "--ntasks=1", "--cpus-per-task=2", "--mem=4G", "--time=02:00:00"):
            self.assertIn(flag, submitted)
        self.assertFalse(any(arg.startswith(("--account=", "--partition=")) for arg in submitted))
        marker = next(arg.split("=", 1)[1] for arg in submitted if arg.startswith("--job-name="))
        self.assertRegex(marker, r"^codex-hpc-[0-9a-f]{32}$")
        self.assertIn("Session job name: " + marker, result.stdout)
        self.assertIn("account=Slurm default", result.stdout)
        self.assertIn("partition=Slurm default", result.stdout)
        recovery = next(line.removeprefix("Cancel: ") for line in result.stdout.splitlines()
                        if line.startswith("Cancel: "))
        self.assertEqual(shlex.split(recovery), ["ssh", "test-login", "scancel --name=" + marker + " 321"])
        self.assertEqual(events[-1]["args"], ["--name=" + marker, "321"])
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
        # This fixture has no compute-node tools/platform; launch failure still
        # follows the same exact owned-allocation cleanup rule.
        self.assertNotEqual(result.returncode, 0)

    def test_resource_overrides_and_environment_defaults_are_literal(self):
        self.env.update(CODEX_HPC_CPUS="7", CODEX_HPC_MEM="8G", CODEX_HPC_TIME="00:30:00",
                        CODEX_HPC_ACCOUNT="environment-account", CODEX_HPC_PARTITION="environment-partition",
                        SBATCH_ARRAY_INX="1-10", SBATCH_JOB_NAME="injected", SCANCEL_SIGNAL="KILL")
        result = self.connect("--cpus", "3", "--mem", "5G", "--time", "01:02:03",
                              "--account", "literal';$(touch INJECTED)", "--partition", "compute")
        submitted = self.scheduler_events()[0]["args"]
        for flag in ("--cpus-per-task=3", "--mem=5G", "--time=01:02:03",
                     "--account=literal';$(touch INJECTED)", "--partition=compute"):
            self.assertIn(flag, submitted)
        self.assertIn("cpus=3 mem=5G time=01:02:03", result.stdout)
        self.assertFalse((self.home / "INJECTED").exists())
        self.assertTrue(all(event["ambient"] == {} for event in self.scheduler_events()))
        Path(self.env["SCHEDULER_LOG"]).unlink()
        self.connect()
        submitted = self.scheduler_events()[0]["args"]
        for flag in ("--cpus-per-task=7", "--mem=8G", "--time=00:30:00",
                     "--account=environment-account", "--partition=environment-partition"):
            self.assertIn(flag, submitted)

    def test_pending_wait_is_bounded_deduplicated_and_independent_of_runtime_limit(self):
        self.env["STATES"] = json.dumps([["PENDING", "Priority"], ["PENDING", "Priority"],
                                          ["PENDING", "Resources"], ["RUNNING", "None"]])
        result = self.connect("--time", "00:00:01")
        polls = [event for event in self.scheduler_events() if event["tool"] == "squeue"]
        self.assertEqual(len(polls), 4)
        for earlier, later in zip(polls, polls[1:]):
            self.assertGreaterEqual(later["time"] - earlier["time"], 5)
        self.assertEqual(result.stdout.count("PENDING (Priority)"), 1)
        self.assertEqual(result.stdout.count("PENDING (Resources)"), 1)
        self.assertIn("RUNNING", result.stdout)
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})

    def test_cluster_qualified_job_id_keeps_poll_and_cleanup_in_same_cluster(self):
        self.env["JOB_RESPONSE"] = "321;cluster-a"
        result = self.connect()
        events = self.scheduler_events()
        self.assertEqual([event["tool"] for event in events], ["sbatch", "squeue", "scancel"])
        self.assertIn("--clusters=cluster-a", events[1]["args"])
        self.assertIn("--clusters=cluster-a", events[2]["args"])
        self.assertIn("--clusters=cluster-a", result.stdout)
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})

    def test_failed_vanished_and_reused_jobs_stop_without_harming_unrelated_jobs(self):
        for state, message in (("FAILED", "state FAILED"), ("VANISHED", "disappeared"),
                               ("REUSED", "identity differs"), ("QUERY_ERROR", "cannot query")):
            with self.subTest(state=state):
                Path(self.env["JOBS"]).write_text(json.dumps({"777": "unrelated-job"}))
                Path(self.env["SCHEDULER_LOG"]).unlink(missing_ok=True)
                self.env["STATES"] = json.dumps([[state, "reason"]])
                result = self.connect()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                jobs = json.loads(Path(self.env["JOBS"]).read_text())
                expected = {"777": "unrelated-job"}
                if state == "REUSED":
                    expected["321"] = "unrelated-replacement"
                self.assertEqual(jobs, expected)
                self.assertEqual([event["tool"] for event in self.scheduler_events()],
                                 ["sbatch", "squeue", "scancel"])

    def test_lost_submission_response_retains_marker_without_retry_or_broad_cancel(self):
        self.env["LOST_RESPONSE"] = "1"
        result = self.connect()
        events = self.scheduler_events()
        self.assertEqual([event["tool"] for event in events], ["sbatch"])
        marker = json.loads(Path(self.env["JOBS"]).read_text())["321"]
        self.assertIn("Session job name: " + marker, result.stdout)
        self.assertIn("squeue --name=" + marker, result.stdout)
        self.assertIn("response was lost", result.stderr)
        self.assertNotIn("Cancel:", result.stdout)

    def test_received_id_is_recovered_even_when_submission_transport_fails(self):
        self.env["SUBMIT_EXIT"] = "255"
        result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cancel:", result.stdout)
        self.assertEqual([event["tool"] for event in self.scheduler_events()], ["sbatch", "scancel"])
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})

    def test_missing_cleanup_tool_stops_before_submission(self):
        self.script("python3", '''
import shutil, sys
original = shutil.which
shutil.which = lambda name: None if name == "scancel" else original(name)
if sys.argv[1] == "-c":
    code = sys.argv[2]
    sys.argv = ["-c", *sys.argv[3:]]
else:
    sys.argv.pop(1)
    code = sys.stdin.read()
exec(compile(code, "<remote>", "exec"))
''')
        result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing: scancel", result.stderr)
        self.assertEqual(self.scheduler_events(), [])

    def test_cleanup_failure_keeps_exact_recovery_without_broader_retry(self):
        self.script("scancel", '''
import json, os, sys
with open(os.environ["SCHEDULER_LOG"], "a") as log:
    log.write(json.dumps({"tool": "scancel", "args": sys.argv[1:]}) + "\\n")
raise SystemExit(1)
''')
        result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cleanup failed", result.stderr)
        self.assertIn("Cancel:", result.stdout)
        self.assertEqual([event["tool"] for event in self.scheduler_events()], ["sbatch", "squeue", "scancel"])
        self.assertEqual(set(json.loads(Path(self.env["JOBS"]).read_text())), {"321", "777"})

    def interrupt_connect(self, after_id):
        stdout = self.root / "stdout"
        stderr = self.root / "stderr"
        with stdout.open("w") as out, stderr.open("w") as err:
            process = subprocess.Popen(self.connect_command(), env=self.env, cwd=self.root,
                                       stdout=out, stderr=err, start_new_session=True)
            try:
                deadline = time.monotonic() + 15
                while process.poll() is None and time.monotonic() < deadline:
                    ready = "Cancel:" in stdout.read_text() if after_id else bool(self.scheduler_events())
                    if ready:
                        break
                    time.sleep(0.02)
                self.assertTrue(ready, stdout.read_text() + stderr.read_text())
                interrupted = time.monotonic()
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=5)
                self.assertLess(time.monotonic() - interrupted, 5)
                self.assertEqual(process.returncode, 130, stderr.read_text())
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        return stdout.read_text(), stderr.read_text()

    def test_ctrl_c_before_id_keeps_marker_recovery_without_resubmission(self):
        self.env["SUBMIT_DELAY"] = "1"
        stdout, stderr = self.interrupt_connect(after_id=False)
        self.assertEqual([event["tool"] for event in self.scheduler_events()], ["sbatch"])
        self.assertIn("Session job name:", stdout)
        self.assertIn("Find after a lost response:", stdout)
        self.assertEqual(set(json.loads(Path(self.env["JOBS"]).read_text())), {"321", "777"})

    def test_ctrl_c_after_id_immediately_cancels_only_owned_job(self):
        self.env["STATES"] = json.dumps([["PENDING", "Priority"]])
        stdout, stderr = self.interrupt_connect(after_id=True)
        events = self.scheduler_events()
        self.assertEqual(events[0]["tool"], "sbatch")
        self.assertEqual(events[-1]["tool"], "scancel")
        self.assertEqual(sum(event["tool"] == "sbatch" for event in events), 1)
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})
