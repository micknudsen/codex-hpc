"""Release lifetime through interleaved installed setup/connect commands."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from test_compute import ComputeFixture


class LifetimeCLI(ComputeFixture):
    def setUp(self):
        super().setUp()
        scheduler = '''
import fcntl, json, os, sys, time
from pathlib import Path
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
def option(name):
    return next((arg.split("=", 1)[1] for arg in args if arg.startswith(name + "=")), None)
jobs_path = Path(os.environ["JOBS"])
with jobs_path.with_suffix(".lock").open("a") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    jobs = json.loads(jobs_path.read_text())
    job_id = os.environ.get("TEST_JOB_ID", "321")
    if tool == "sbatch":
        jobs[job_id] = option("--job-name")
        if not os.environ.get("LOST_RESPONSE"): print(job_id, flush=True)
    elif tool == "scancel":
        if jobs.get(args[-1]) == option("--name"): jobs.pop(args[-1])
    elif tool == "squeue" and not os.environ.get("QUERY_FAILURE"):
        state = Path(os.environ["STATE_FILE"]).read_text() if os.environ.get("STATE_FILE") else "RUNNING"
        for identity, marker in jobs.items():
            if ((option("--jobs") and identity == option("--jobs")) or
                    (option("--name") and marker == option("--name"))):
                print("|".join([identity, marker, state, "None", "node01"]))
    jobs_path.write_text(json.dumps(jobs))
    with open(os.environ["SCHEDULER_LOG"], "a") as log:
        log.write(json.dumps({"tool": tool, "args": args, "time": time.time()}) + "\\n")
if tool == "squeue" and os.environ.get("QUERY_FAILURE"): raise SystemExit(1)
if tool == "sbatch" and os.environ.get("LOST_RESPONSE"): raise SystemExit(255)
'''
        for tool in ("sbatch", "squeue", "scancel"):
            self.script(tool, scheduler)
        step = (self.bin / "srun").read_text().replace(
            'os.environ["SLURM_JOB_ID"] = "321"',
            'os.environ["SLURM_JOB_ID"] = os.environ.get("TEST_JOB_ID", "321")')
        (self.bin / "srun").write_text(step)
        container = (self.bin / "apptainer").read_text().replace(
            "    time.sleep(60)",
            '    while not Path(os.environ["SESSION_EXIT"]).exists(): time.sleep(0.02)')
        (self.bin / "apptainer").write_text(container)

    def wait_for(self, predicate, process=None):
        deadline = time.monotonic() + 15
        while not predicate() and time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                break
            time.sleep(0.02)
        self.assertTrue(predicate())

    def start_connect(self, name="session", environment=None):
        out = (self.root / (name + ".out")).open("w")
        err = (self.root / (name + ".err")).open("w")
        self.addCleanup(out.close)
        self.addCleanup(err.close)
        process = subprocess.Popen(self.connect_command(), env=environment or self.env,
                                   cwd=self.root, stdout=out, stderr=err, start_new_session=True)
        def cleanup():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        self.addCleanup(cleanup)
        return process

    def test_upgrade_collects_unused_helper_but_retains_active_shared_image(self):
        old = (self.data / "current").resolve()
        image = Path(json.loads((old / "installation.json").read_text())["image"])
        unrelated = self.data / "releases/unrelated"
        unrelated.mkdir()
        (unrelated / "helper.py").write_text("unrelated helper")
        unrelated_image = image.parent / "unrelated.sif"
        unrelated_image.write_text("unrelated image")
        self.release["version"] = "0.2.0"
        self.save_release()
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists(), "unused obsolete helper survived activation")
        self.assertTrue(image.is_file(), "active image was collected with zero sessions")
        self.assertNotEqual((self.data / "current").resolve(), old)
        self.assertEqual((unrelated / "helper.py").read_text(), "unrelated helper")
        self.assertEqual(unrelated_image.read_text(), "unrelated image")
        self.assertEqual(len([event for event in self.events() if event.startswith("fetch ")]), 1)

    def test_upgrade_between_preflight_and_submission_preserves_selected_release(self):
        old = (self.data / "current").resolve()
        image = Path(json.loads((old / "installation.json").read_text())["image"])
        original = (self.bin / "sbatch").read_text().split("\n", 1)[1]
        gate = self.root / "before-submit"
        proceed = self.root / "submit-now"
        self.env.update(SUBMIT_GATE=str(gate), SUBMIT_PROCEED=str(proceed))
        self.script("sbatch", '''
import os, time
from pathlib import Path
Path(os.environ["SUBMIT_GATE"]).touch()
while not Path(os.environ["SUBMIT_PROCEED"]).exists(): time.sleep(0.02)
''' + original)
        process = self.start_connect()
        self.wait_for(gate.exists, process)
        self.assertIn("Preflight passed.", (self.root / "session.out").read_text())
        self.assertEqual(self.scheduler_events(), [])
        self.release["version"] = "0.2.0"
        self.save_release()
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(old.is_dir(), "upgrade retired preflight's selected helper")
        self.assertTrue(image.is_file())
        proceed.touch()
        self.assertEqual(process.wait(timeout=20), 0, (self.root / "session.err").read_text())
        self.assertFalse(old.exists(), "final session exit did not collect obsolete helper")
        self.assertTrue(image.is_file())
        launched = [event for event in self.compute_events() if "srun" in event]
        self.assertTrue(all(str(old / "helper.py") in event["srun"] for event in launched))

    def test_cache_switch_collects_unreferenced_image_but_preserves_other_cache_owner(self):
        old = (self.data / "current").resolve()
        image = Path(json.loads((old / "installation.json").read_text())["image"])
        other_data = self.home / "another installation"
        self.env["CODEX_HPC_DATA_ROOT"] = str(other_data)
        self.assertEqual(self.setup().returncode, 0)
        self.env.pop("CODEX_HPC_DATA_ROOT")
        self.env["CODEX_HPC_CACHE_ROOT"] = str(self.home / "new cache")
        self.release["version"] = "0.2.0"
        self.save_release()
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(image.exists(), "another active installation still owns this image")
        self.env["CODEX_HPC_DATA_ROOT"] = str(other_data)
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(image.exists(), "last cache owner moved but obsolete image survived")
        current_image = Path(json.loads((other_data / "current/installation.json").read_text())["image"])
        self.assertTrue(current_image.exists())
        self.assertEqual(len([event for event in self.events() if event.startswith("fetch ")]), 2)

    def test_queued_session_retains_release_through_upgrade_and_launch(self):
        old = (self.data / "current").resolve()
        state = self.root / "scheduler-state"
        state.write_text("PENDING")
        self.env["STATE_FILE"] = str(state)
        process = self.start_connect()
        self.wait_for(lambda: "PENDING" in (self.root / "session.out").read_text(), process)
        self.release["version"] = "0.2.0"
        self.save_release()
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(old.exists())
        state.write_text("RUNNING")
        self.assertEqual(process.wait(timeout=20), 0, (self.root / "session.err").read_text())
        self.assertFalse(old.exists())
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})

    def test_last_concurrent_session_collects_obsolete_helper_and_image(self):
        old = (self.data / "current").resolve()
        image = Path(json.loads((old / "installation.json").read_text())["image"])
        exits = [self.root / "exit-one", self.root / "exit-two"]
        processes = []
        for index, end in enumerate(exits):
            environment = dict(self.env, CODEX_WAIT="1", SESSION_EXIT=str(end),
                               TEST_JOB_ID=str(321 + index))
            processes.append(self.start_connect("session-" + str(index), environment))
            self.wait_for(lambda: len([event for event in self.compute_events() if "apptainer" in event])
                          == len(processes), processes[-1])
        self.release["version"] = "0.2.0"
        self.save_release()
        self.env["CODEX_HPC_CACHE_ROOT"] = str(self.home / "new cache")
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(old.exists())
        self.assertTrue(image.exists())
        exits[0].touch()
        self.assertEqual(processes[0].wait(timeout=15), 0)
        self.assertTrue(old.exists(), "first exit retired a concurrently used helper")
        self.assertTrue(image.exists())
        exits[1].touch()
        self.assertEqual(processes[1].wait(timeout=15), 0)
        self.assertFalse(old.exists())
        self.assertFalse(image.exists())
        active_image = Path(json.loads((self.data / "current/installation.json").read_text())["image"])
        self.assertTrue(active_image.exists())
        self.assertEqual(json.loads(Path(self.env["JOBS"]).read_text()), {"777": "unrelated-job"})

    def test_later_setup_recovers_abandoned_lease_only_after_job_is_absent(self):
        old = (self.data / "current").resolve()
        self.env.update(CODEX_WAIT="1", SESSION_EXIT=str(self.root / "never-exit"))
        process = self.start_connect()
        self.wait_for(lambda: any("apptainer" in event for event in self.compute_events()), process)
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        self.release["version"] = "0.2.0"
        self.save_release()
        self.assertEqual(self.setup().returncode, 0)
        self.assertTrue(old.exists(), "lost client was mistaken for a finished scheduler job")
        Path(self.env["JOBS"]).write_text(json.dumps({"777": "unrelated-job"}))
        self.env["QUERY_FAILURE"] = "1"
        self.assertEqual(self.setup().returncode, 0)
        self.assertTrue(old.exists(), "scheduler query failure was mistaken for absence")
        self.env.pop("QUERY_FAILURE")
        lease_record = next((self.data / ".leases").glob("*/release.json"))
        original = lease_record.read_text()
        lease_record.write_text(json.dumps({"schema": 1, "identity": "unreadable-identity"}))
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cleanup deferred", result.stderr)
        self.assertTrue(old.exists(), "unreadable lease was treated as proof of no use")
        lease_record.write_text(original)
        self.assertEqual(self.setup().returncode, 0)
        self.assertFalse(old.exists())
        self.assertEqual(list((self.data / ".leases").iterdir()), [])

    def test_lost_submission_response_is_retained_then_recovered_after_observed_job_exit(self):
        old = (self.data / "current").resolve()
        self.env["LOST_RESPONSE"] = "1"
        result = self.connect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("response was lost", result.stderr)
        self.release["version"] = "0.2.0"
        self.save_release()
        self.assertEqual(self.setup().returncode, 0)
        self.assertTrue(old.exists())
        Path(self.env["JOBS"]).write_text(json.dumps({"777": "unrelated-job"}))
        self.assertEqual(self.setup().returncode, 0)
        self.assertFalse(old.exists())

    def test_interrupted_collection_is_retried_without_losing_ownership(self):
        old = (self.data / "current").resolve()
        interpreter = (self.bin / "python3").read_text()
        self.env["REFUSE_REMOVAL"] = old.name
        inject = '''
original_rmdir = os.rmdir
def interrupted_removal(path, *args, **kwargs):
    if str(path).endswith(os.environ.get("REFUSE_REMOVAL", "never-match")):
        raise OSError("simulated interrupted removal")
    return original_rmdir(path, *args, **kwargs)
os.rmdir = interrupted_removal
'''
        (self.bin / "python3").write_text(interpreter.replace("sys.argv.pop(0)", inject + "\nsys.argv.pop(0)"))
        self.release["version"] = "0.2.0"
        self.save_release()
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cleanup deferred", result.stderr)
        self.assertEqual(json.loads((self.data / "current/release.json").read_text())["version"], "0.2.0")
        self.assertTrue(old.exists())
        (self.bin / "python3").write_text(interpreter)
        result = self.setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old.exists(), "partially removed release lost its managed ownership")
