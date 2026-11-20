"""Local side of compute-node sign-in and its owned callback forwarding."""
import socket
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlparse
from .transport import ssh_options


def check_callback_port(port=1455):
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    except OSError as error:
        raise ValueError("browser callback port " + str(port) +
                         " is busy; free the port and rerun connect") from error


def callback_port(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "auth.openai.com":
        raise ValueError("unexpected Codex sign-in URL")
    redirects = parse_qs(parsed.query).get("redirect_uri", [])
    if len(redirects) != 1:
        raise ValueError("sign-in URL has no unique callback")
    redirect = urlparse(redirects[0])
    if (redirect.scheme != "http" or redirect.hostname != "127.0.0.1"
            or redirect.port not in (1455, 1457) or redirect.path != "/auth/callback"):
        raise ValueError("unsupported Codex callback address")
    return redirect.port


class CallbackTunnel:
    def __init__(self, host, jump):
        self.host = host
        self.jump = jump
        self.directory = None
        self.control = None

    def open(self, port):
        check_callback_port(port)
        # OpenSSH control paths have a short platform-dependent maximum length.
        self.directory = Path(tempfile.mkdtemp(prefix="chpc-", dir="/tmp"))
        self.control = str(self.directory / "ssh")
        command = [*ssh_options(self.jump), "-f", "-N", "-T", "-M", "-S", self.control,
                   "-o", "ControlPersist=no", "-o", "ExitOnForwardFailure=yes",
                   "-L", "127.0.0.1:" + str(port) + ":127.0.0.1:" + str(port),
                   "--", self.host]
        # -f exits successfully only after authentication and forwarding setup;
        # ExitOnForwardFailure prevents a failed bind from opening a browser.
        terminal = None
        try:
            try:
                terminal = open("/dev/tty", "w")
            except OSError:
                pass
            # A detached master must not hold the client's captured output pipes
            # open. Interactive SSH diagnostics still use the controlling tty.
            result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                    stderr=terminal if terminal is not None else subprocess.DEVNULL)
        finally:
            if terminal is not None:
                terminal.close()
        if result.returncode:
            raise ValueError("cannot establish callback tunnel on port " + str(port) +
                             "; free the port, check SSH forwarding and rerun connect")

    def close(self):
        if self.control is not None and Path(self.control).exists():
            command = ["ssh", "-S", self.control, "-O", "exit", "--", self.host]
            try:
                result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=10)
                closed = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                closed = False
            if not closed:
                # Keep the explicit socket usable for recovery; never report a
                # completed sign-in while its forwarding master is unconfirmed.
                raise ValueError("callback tunnel shutdown failed. Close it with: " + shlex.join(command))
        if self.directory is not None:
            shutil.rmtree(self.directory)
            self.directory = None
            self.control = None


class BrowserLaunch:
    """Observe launcher failure without waiting for the browser application."""

    def __init__(self):
        self.process = None
        self.url = None

    def start(self, url):
        self.url = url
        try:
            self.process = subprocess.Popen([
                sys.executable, "-c", "import sys, webbrowser; "
                "sys.exit(0 if webbrowser.get().open(sys.argv[1], new=1) else 1)", url],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError:
            self.manual()

    def manual(self):
        print("Open this sign-in URL in a browser on this computer:\n" + self.url, flush=True)
        print("Keeping the callback tunnel open until sign-in completes.", flush=True)

    def poll(self):
        if self.process is not None and self.process.poll() is not None:
            if self.process.returncode:
                self.manual()
            self.process = None

    def close(self):
        if self.process is not None:
            # Stop only the launcher wrapper. The user's browser may stay open.
            stop_process(self.process)
            self.process = None


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def authenticate(command, host, jump):
    tunnel = CallbackTunnel(host, jump)
    browser = BrowserLaunch()
    process = None
    completed = False
    try:
        # Preserve the controlling terminal for normal SSH host-key/password
        # prompts. Only remote login stdin is noninteractive.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        pending = b""
        while True:
            browser.poll()
            if not select.select([process.stdout], [], [], 0.05)[0]:
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                if pending.strip():
                    raise ValueError("incomplete sign-in response")
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                event = json.loads(line)
                if not isinstance(event, dict) or completed:
                    raise ValueError("unexpected sign-in response")
                if event.get("event") == "browser" and browser.url is None:
                    url = event.get("url")
                    if not isinstance(url, str):
                        raise ValueError("invalid sign-in URL")
                    tunnel.open(callback_port(url))
                    browser.start(url)
                elif event.get("event") == "message" and isinstance(event.get("text"), str):
                    print(event["text"], flush=True)
                elif event.get("event") == "complete":
                    tunnel.close()
                    browser.close()
                    completed = True
                else:
                    raise ValueError("unexpected sign-in response")
        status = process.wait()
        if status or not completed:
            raise ValueError("compute sign-in failed; rerun connect to retry")
    finally:
        try:
            tunnel.close()
        finally:
            browser.close()
            if process is not None:
                stop_process(process)
                process.stdout.close()
