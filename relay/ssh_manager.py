"""
SSH connection manager for the relay daemon.

Maintains a persistent SSH connection to the jumpbox and provides:
- Command execution on the jumpbox
- Two-hop command execution on target servers (via jumpbox)
- SFTP file read/write on the jumpbox

Supports multiple target servers concurrently — each target gets its own
cached SSH connection, keyed by hostname. Thread-safe via locks.

Host key handling: Target servers get redeployed by DCI (fresh OS = new SSH
host key every time). We intentionally never load system known_hosts and use
AutoAddPolicy so Paramiko always accepts whatever key the target presents.
"""

import logging
import os
import subprocess
import threading

import paramiko

from . import config

logger = logging.getLogger(__name__)


class SSHManager:
    """
    Manages persistent SSH connections to the jumpbox and target servers.

    The jumpbox connection is established once and reused. Target servers
    are reached through the jumpbox using forwarded SSH channels, with
    one cached connection per target hostname.

    Target host keys are never cached -- the target gets redeployed (fresh OS)
    on every DCI run, so its host key changes each time. We never call
    load_system_host_keys() or load_host_keys() to avoid stale key conflicts.
    """

    def __init__(self):
        self._jumpbox: paramiko.SSHClient | None = None
        self._jumpbox_control: paramiko.SSHClient | None = None
        self._targets: dict[str, paramiko.SSHClient] = {}
        self._jumpbox_lock = threading.Lock()
        self._jumpbox_control_lock = threading.Lock()
        self._targets_lock = threading.Lock()
        self._keepalive_stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None

    def start_keepalive(self, interval: int = 60):
        """Start a background thread that proactively tests the control connection."""
        if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
            return
        self._keepalive_stop.clear()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, args=(interval,),
            name="ssh-keepalive", daemon=True,
        )
        self._keepalive_thread.start()
        logger.info("SSH keepalive thread started (interval=%ds)", interval)

    def stop_keepalive(self):
        """Stop the background keepalive thread."""
        if self._keepalive_thread is None:
            return
        self._keepalive_stop.set()
        self._keepalive_thread.join(timeout=5)
        self._keepalive_thread = None

    def _keepalive_loop(self, interval: int):
        """Periodically test and reconnect the control SSH connection."""
        while not self._keepalive_stop.wait(interval):
            try:
                with self._jumpbox_control_lock:
                    if self._jumpbox_control is None:
                        continue
                    try:
                        self._jumpbox_control.exec_command("echo ok", timeout=5)
                    except Exception:
                        logger.info("Keepalive: control connection dead, reconnecting")
                        try:
                            self._jumpbox_control.close()
                        except Exception:
                            pass
                        self._jumpbox_control = None
                        client = paramiko.SSHClient()
                        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                        connect_kwargs: dict = {
                            "hostname": config.JUMPBOX_HOST,
                            "username": config.JUMPBOX_USER,
                            "timeout": 15,
                        }
                        if config.JUMPBOX_SSH_KEY:
                            connect_kwargs["key_filename"] = config.JUMPBOX_SSH_KEY
                        client.connect(**connect_kwargs)
                        transport = client.get_transport()
                        if transport:
                            transport.set_keepalive(30)
                        self._jumpbox_control = client
                        logger.info("Keepalive: control connection restored")
            except Exception as e:
                logger.warning("Keepalive: reconnect failed: %s", e)

    @staticmethod
    def clear_known_host(hostname: str) -> None:
        """Remove a host from the system known_hosts file.

        Called before connecting to the target server since DCI redeploys
        give it a new host key every time. Also useful if someone SSHes
        to the target manually from this machine.
        """
        known_hosts = os.path.expanduser("~/.ssh/known_hosts")
        if not os.path.exists(known_hosts):
            return
        try:
            subprocess.run(
                ["ssh-keygen", "-R", hostname],
                capture_output=True, timeout=5,
            )
            logger.info("Cleared stale host key for %s from known_hosts", hostname)
        except Exception as e:
            logger.debug("Could not clear known_hosts for %s: %s", hostname, e)

    def _connect_jumpbox(self) -> paramiko.SSHClient:
        """Establish or re-establish the SSH connection to the jumpbox."""
        with self._jumpbox_lock:
            if self._jumpbox is not None:
                try:
                    self._jumpbox.exec_command("echo ok", timeout=5)
                    return self._jumpbox
                except Exception:
                    logger.info("Jumpbox connection stale, reconnecting")
                    try:
                        self._jumpbox.close()
                    except Exception:
                        pass
                    self._jumpbox = None

            logger.info("Connecting to jumpbox %s@%s", config.JUMPBOX_USER, config.JUMPBOX_HOST)
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            connect_kwargs: dict = {
                "hostname": config.JUMPBOX_HOST,
                "username": config.JUMPBOX_USER,
                "timeout": 30,
            }
            if config.JUMPBOX_SSH_KEY:
                connect_kwargs["key_filename"] = config.JUMPBOX_SSH_KEY
            client.connect(**connect_kwargs)

            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)

            self._jumpbox = client
            logger.info("Connected to jumpbox")
            return client

    def _connect_jumpbox_control(self) -> paramiko.SSHClient:
        """Establish or re-establish a SEPARATE SSH connection for control commands.

        This connection is independent of _jumpbox (used by streaming/workflow).
        Short-lived commands (stop, list, ping, diagnostics) use this so they
        never block on the workflow thread's transport lock.
        """
        with self._jumpbox_control_lock:
            if self._jumpbox_control is not None:
                try:
                    self._jumpbox_control.exec_command("echo ok", timeout=5)
                    return self._jumpbox_control
                except Exception:
                    logger.info("Control connection stale, reconnecting")
                    try:
                        self._jumpbox_control.close()
                    except Exception:
                        pass
                    self._jumpbox_control = None

            logger.info("Opening control connection to jumpbox %s@%s", config.JUMPBOX_USER, config.JUMPBOX_HOST)
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            connect_kwargs: dict = {
                "hostname": config.JUMPBOX_HOST,
                "username": config.JUMPBOX_USER,
                "timeout": 30,
            }
            if config.JUMPBOX_SSH_KEY:
                connect_kwargs["key_filename"] = config.JUMPBOX_SSH_KEY
            client.connect(**connect_kwargs)

            transport = client.get_transport()
            if transport:
                transport.set_keepalive(30)

            self._jumpbox_control = client
            logger.info("Control connection established")
            return client

    def _connect_target(
        self,
        target_host: str = "",
        target_user: str = "",
        target_password: str = "",
    ) -> paramiko.SSHClient:
        """
        Establish or re-establish SSH to a target via the jumpbox.

        Each target hostname gets its own cached connection. Thread-safe.

        Target servers get a fresh OS on every DCI deployment, so their
        SSH host key changes every time. We:
        1. Never load system known_hosts (Paramiko starts with empty dict)
        2. Clear the target from system known_hosts before connecting
        3. Use AutoAddPolicy to accept whatever key the target presents
        """
        target_host = target_host or config.TARGET_HOST
        target_user = target_user or config.TARGET_USER
        target_password = target_password or config.get_target_password(target_host)

        with self._targets_lock:
            cached = self._targets.get(target_host)
            if cached is not None:
                try:
                    cached.exec_command("echo ok", timeout=5)
                    return cached
                except Exception:
                    logger.info("Target %s connection stale, reconnecting", target_host)
                    try:
                        cached.close()
                    except Exception:
                        pass
                    del self._targets[target_host]

        self.clear_known_host(target_host)

        jumpbox = self._connect_jumpbox_control()
        jumpbox_transport = jumpbox.get_transport()
        if jumpbox_transport is None:
            raise ConnectionError("Jumpbox transport is None")

        logger.info("Opening channel to target %s via jumpbox", target_host)
        channel = jumpbox_transport.open_channel(
            "direct-tcpip",
            (target_host, 22),
            ("127.0.0.1", 0),
        )

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict = {
            "hostname": target_host,
            "username": target_user,
            "sock": channel,
            "timeout": 30,
        }
        if target_password:
            connect_kwargs["password"] = target_password
            connect_kwargs["look_for_keys"] = False
        client.connect(**connect_kwargs)

        transport = client.get_transport()
        if transport:
            transport.set_keepalive(30)

        with self._targets_lock:
            self._targets[target_host] = client
        logger.info("Connected to target %s", target_host)
        return client

    def exec_on_jumpbox(self, command: str, timeout: int = 120, get_pty: bool = False) -> dict:
        """Execute a command on the jumpbox via the control connection.

        Uses a separate SSH connection from the workflow streaming connection
        so short commands never block on a running workflow.
        """
        try:
            client = self._connect_jumpbox_control()
            _, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=get_pty)
            exit_code = stdout.channel.recv_exit_status()
            return {
                "command": command,
                "exit_code": exit_code,
                "stdout": stdout.read().decode("utf-8", errors="replace"),
                "stderr": stderr.read().decode("utf-8", errors="replace"),
                "success": exit_code == 0,
            }
        except Exception as e:
            logger.error("Jumpbox exec failed: %s", e)
            with self._jumpbox_control_lock:
                self._jumpbox_control = None
            return {
                "command": command,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "success": False,
            }

    def _open_workflow_connection(self) -> paramiko.SSHClient:
        """Open a dedicated SSH connection for a workflow.

        Each streaming workflow gets its own connection so multiple
        workflows can run in parallel without blocking each other.
        The caller is responsible for closing the connection.
        """
        logger.info("Opening workflow connection to jumpbox %s@%s", config.JUMPBOX_USER, config.JUMPBOX_HOST)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict = {
            "hostname": config.JUMPBOX_HOST,
            "username": config.JUMPBOX_USER,
            "timeout": 30,
        }
        if config.JUMPBOX_SSH_KEY:
            connect_kwargs["key_filename"] = config.JUMPBOX_SSH_KEY
        client.connect(**connect_kwargs)

        transport = client.get_transport()
        if transport:
            transport.set_keepalive(30)

        logger.info("Workflow connection established")
        return client

    def exec_on_jumpbox_streaming(
        self,
        command: str,
        timeout: int = 7200,
        get_pty: bool = False,
        line_callback: callable = None,
    ) -> dict:
        """Execute a long-running command on the jumpbox with streaming output.

        Opens a dedicated SSH connection per call so multiple workflows
        can run in parallel. The connection is closed when the command
        finishes. Enforces a wall-clock timeout so the thread never
        blocks forever.

        Returns the same dict format as exec_on_jumpbox.
        """
        import time as _time

        client = None
        try:
            client = self._open_workflow_connection()
            transport = client.get_transport()
            if transport is None:
                raise ConnectionError("Jumpbox transport is None")

            channel = transport.open_session()
            if get_pty:
                channel.get_pty()
            channel.settimeout(timeout)
            channel.exec_command(command)

            _STDOUT_CAP = 1_048_576  # 1MB — play recap and failures are near the end
            stdout_chunks = []
            stdout_size = 0
            stderr_chunks = []
            line_buf = ""
            deadline = _time.time() + timeout

            while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
                if _time.time() > deadline:
                    logger.error("Wall-clock timeout (%ds) exceeded, closing channel", timeout)
                    channel.close()
                    return {
                        "command": command,
                        "exit_code": -1,
                        "stdout": "".join(stdout_chunks)[-_STDOUT_CAP:],
                        "stderr": f"Wall-clock timeout ({timeout}s) exceeded. Process may still be running on jumpbox.",
                        "success": False,
                    }

                if channel.recv_ready():
                    data = channel.recv(65536).decode("utf-8", errors="replace")
                    stdout_chunks.append(data)
                    stdout_size += len(data)
                    if stdout_size > _STDOUT_CAP * 2:
                        joined = "".join(stdout_chunks)
                        stdout_chunks = [joined[-_STDOUT_CAP:]]
                        stdout_size = len(stdout_chunks[0])
                    if line_callback:
                        line_buf += data
                        while "\n" in line_buf:
                            line, line_buf = line_buf.split("\n", 1)
                            line = line.rstrip("\r")
                            if line:
                                line_callback(line)

                if channel.recv_stderr_ready():
                    stderr_chunks.append(
                        channel.recv_stderr(65536).decode("utf-8", errors="replace")
                    )

                if not channel.recv_ready() and not channel.recv_stderr_ready():
                    _time.sleep(0.1)

            if line_buf and line_callback:
                line_callback(line_buf.rstrip("\r"))

            exit_code = channel.recv_exit_status()
            channel.close()

            return {
                "command": command,
                "exit_code": exit_code,
                "stdout": "".join(stdout_chunks)[-_STDOUT_CAP:],
                "stderr": "".join(stderr_chunks),
                "success": exit_code == 0,
            }
        except Exception as e:
            logger.error("Jumpbox streaming exec failed: %s", e)
            return {
                "command": command,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "success": False,
            }
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    FALLBACK_PASSWORDS = [
        p.strip() for p in
        os.environ.get("DCI_FALLBACK_PASSWORDS", "").split(",")
        if p.strip()
    ]

    def exec_on_target(self, command: str, target_host: str = "", timeout: int = 120) -> dict:
        """
        Execute a command on a target server via the jumpbox (two-hop).

        If authentication fails, automatically tries fallback passwords
        (from DCI_FALLBACK_PASSWORDS env var) before giving up.
        Returns dict with command, exit_code, stdout, stderr, success.
        """
        target_host = target_host or config.TARGET_HOST
        try:
            client = self._connect_target(target_host=target_host)
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            return {
                "command": command,
                "exit_code": exit_code,
                "stdout": stdout.read().decode("utf-8", errors="replace")[:5000],
                "stderr": stderr.read().decode("utf-8", errors="replace")[:2000],
                "success": exit_code == 0,
            }
        except Exception as e:
            err_str = str(e).lower()
            is_auth_failure = "authentication" in err_str or "auth" in err_str or "password" in err_str

            if is_auth_failure:
                logger.warning("Auth failed for %s with configured password, trying fallbacks", target_host)
                with self._targets_lock:
                    self._targets.pop(target_host, None)

                primary_pw = config.get_target_password(target_host)
                for fallback_pw in self.FALLBACK_PASSWORDS:
                    if fallback_pw == primary_pw:
                        continue
                    try:
                        logger.info("Trying fallback password for %s", target_host)
                        client = self._connect_target(
                            target_host=target_host,
                            target_password=fallback_pw,
                        )
                        _, stdout, stderr = client.exec_command(command, timeout=timeout)
                        exit_code = stdout.channel.recv_exit_status()
                        logger.info("Fallback password worked for %s", target_host)
                        return {
                            "command": command,
                            "exit_code": exit_code,
                            "stdout": stdout.read().decode("utf-8", errors="replace")[:5000],
                            "stderr": stderr.read().decode("utf-8", errors="replace")[:2000],
                            "success": exit_code == 0,
                            "_auth_note": "Connected with fallback password (configured password failed)",
                        }
                    except Exception:
                        with self._targets_lock:
                            self._targets.pop(target_host, None)
                        continue

            logger.error("Target exec failed (%s): %s", target_host, e)
            with self._targets_lock:
                self._targets.pop(target_host, None)
            return {
                "command": command,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "success": False,
            }

    def sftp_read(self, remote_path: str) -> str:
        """Read a file from the jumpbox via SFTP."""
        client = self._connect_jumpbox_control()
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "r") as f:
                return f.read().decode("utf-8", errors="replace")
        finally:
            sftp.close()

    def sftp_write(self, remote_path: str, content: str) -> None:
        """Write content to a file on the jumpbox via SFTP."""
        client = self._connect_jumpbox_control()
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "w") as f:
                f.write(content.encode("utf-8"))
        finally:
            sftp.close()

    def sftp_stat(self, remote_path: str) -> dict | None:
        """Stat a file on the jumpbox. Returns None if not found."""
        client = self._connect_jumpbox_control()
        sftp = client.open_sftp()
        try:
            attrs = sftp.stat(remote_path)
            return {
                "size": attrs.st_size,
                "mtime": attrs.st_mtime,
                "mode": attrs.st_mode,
            }
        except (FileNotFoundError, IOError, OSError):
            return None
        finally:
            sftp.close()

    def close(self):
        """Close all SSH connections."""
        self.stop_keepalive()
        with self._targets_lock:
            for hostname, conn in self._targets.items():
                try:
                    conn.close()
                    logger.info("Closed target connection to %s", hostname)
                except Exception:
                    pass
            self._targets.clear()

        with self._jumpbox_lock:
            if self._jumpbox is not None:
                try:
                    self._jumpbox.close()
                    logger.info("Closed jumpbox workflow connection")
                except Exception:
                    pass
                self._jumpbox = None

        with self._jumpbox_control_lock:
            if self._jumpbox_control is not None:
                try:
                    self._jumpbox_control.close()
                    logger.info("Closed jumpbox control connection")
                except Exception:
                    pass
                self._jumpbox_control = None
