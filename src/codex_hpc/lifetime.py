"""Hold a remote release lease across preflight, submission and execution."""
import json
import shlex
import subprocess
import sys

from .transport import ssh_arguments


class ReleaseLease:
    def __init__(self, host, helper, payload):
        self.host = host
        self.helper = helper
        self.payload = {key: value for key, value in payload.items() if key != "helper"}
        self.process = None
        self.recover_cleanup = False

    def __enter__(self):
        source = self.helper.encode("utf-8")
        bootstrap = ("import sys; _bootstrap_source = sys.stdin.buffer.read(" + str(len(source)) +
                     ").decode('utf-8'); exec(compile(_bootstrap_source, '<helper>', 'exec'))")
        command = shlex.join(["python3", "-c", bootstrap, "--lease", json.dumps(self.payload)])
        # Keep the controlling terminal for ordinary SSH authentication prompts.
        self.process = subprocess.Popen(ssh_arguments(self.host, command), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            self.process.stdin.write(self.helper)
            self.process.stdin.flush()
            self.ready = self.read()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def read(self):
        line = self.process.stdout.readline()
        if not line:
            raise OSError("release lease connection lost; inspect the printed job recovery information")
        return json.loads(line)

    def remote(self, operation, arguments):
        try:
            self.process.stdin.write(json.dumps({"operation": operation, "arguments": arguments}) + "\n")
            self.process.stdin.flush()
            response = self.read()
        except OSError:
            if operation != "slurm" or arguments[0] != "scancel":
                raise
            # Ctrl-C can close the lease SSH alongside the foreground session.
            # Retry only exact-ID/marker cancellation, never job submission.
            self.recover_cleanup = True
            return self.once("slurm", arguments)
        if response.get("stderr"):
            print(response["stderr"], end="", file=sys.stderr)
        return subprocess.CompletedProcess(arguments, response["returncode"], response["stdout"])

    def once(self, operation, request):
        command = "python3 - --" + operation + " " + shlex.quote(json.dumps(request))
        return subprocess.run(ssh_arguments(self.host, command), input=self.helper,
                              text=True, stdout=subprocess.PIPE)

    def __exit__(self, *exception):
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Disconnect this SSH only. The durable remote lease remains available
            # for scheduler-aware recovery if the helper cannot finish cleanup.
            self.process.terminate()
            print("codex-hpc: release cleanup deferred; rerun setup to retry.", file=sys.stderr)
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        finally:
            self.process.stdout.close()
        if self.recover_cleanup:
            self.once("collect", self.payload["roots"])
