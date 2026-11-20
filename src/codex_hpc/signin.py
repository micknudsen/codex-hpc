"""Local side of compute-node sign-in and its owned callback forwarding."""
import socket
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from urllib.parse import parse_qs, urlparse
import webbrowser


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
        self.directory = tempfile.TemporaryDirectory(prefix="chpc-", dir="/tmp")
        self.control = str(Path(self.directory.name) / "ssh")
        command = ["ssh", "-f", "-N", "-T", "-M", "-S", self.control,
                   "-o", "ControlPersist=no", "-o", "ExitOnForwardFailure=yes",
                   "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-J", self.jump,
                   "-L", "127.0.0.1:" + str(port) + ":127.0.0.1:" + str(port),
                   "--", self.host]
        # -f exits successfully only after authentication and forwarding setup;
        # ExitOnForwardFailure prevents a failed bind from opening a browser.
        if subprocess.run(command).returncode:
            raise ValueError("cannot establish callback tunnel on port " + str(port) +
                             "; free the port, check SSH forwarding and rerun connect")

    def close(self):
        try:
            if self.control is not None and Path(self.control).exists():
                subprocess.run(["ssh", "-S", self.control, "-O", "exit", "--", self.host],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        finally:
            if self.directory is not None:
                self.directory.cleanup()
                self.directory = None
                self.control = None


def authenticate(command, host, jump):
    tunnel = CallbackTunnel(host, jump)
    process = None
    completed = False
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   text=True, start_new_session=True)
        for line in process.stdout:
            event = json.loads(line)
            if event["event"] == "browser":
                url = event["url"]
                tunnel.open(callback_port(url))
                try:
                    opened = webbrowser.get().open(url, new=1)
                except (webbrowser.Error, OSError):
                    opened = False
                if not opened:
                    print("Open this sign-in URL in a browser on this computer:\n" + url, flush=True)
                    print("Keeping the callback tunnel open until sign-in completes.", flush=True)
            elif event["event"] == "message":
                print(event["text"], flush=True)
            elif event["event"] == "complete":
                tunnel.close()
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
            if process is not None:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                process.stdout.close()
