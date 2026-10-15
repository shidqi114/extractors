from __future__ import annotations

import select
import socketserver
import threading

import paramiko

from .config import SSHConfig


class _ForwardServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    transport: paramiko.Transport
    remote: tuple[str, int]


class _ForwardHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: _ForwardServer = self.server  # type: ignore[assignment]
        try:
            chan = server.transport.open_channel("direct-tcpip", server.remote, self.request.getpeername())
        except Exception:
            return
        try:
            while True:
                readable, _, _ = select.select([self.request, chan], [], [], 1.0)
                if self.request in readable:
                    data = self.request.recv(32768)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in readable:
                    data = chan.recv(32768)
                    if not data:
                        break
                    self.request.sendall(data)
        finally:
            chan.close()
            self.request.close()


class SSHSession:
    """One SSH connection used for port forwarding, SFTP and remote commands."""

    def __init__(self, cfg: SSHConfig):
        self.cfg = cfg
        self._client = paramiko.SSHClient()
        self._servers: list[_ForwardServer] = []
        self._sftp: paramiko.SFTPClient | None = None

    def __enter__(self) -> SSHSession:
        self._client.load_system_host_keys()
        policy = paramiko.RejectPolicy() if self.cfg.strict_host_key else paramiko.AutoAddPolicy()
        self._client.set_missing_host_key_policy(policy)
        self._client.connect(
            self.cfg.host,
            port=self.cfg.port,
            username=self.cfg.user,
            password=self.cfg.password,
            key_filename=self.cfg.key_path,
            look_for_keys=self.cfg.password is None,
            timeout=20,
        )
        self._client.get_transport().set_keepalive(30)  # type: ignore[union-attr]
        return self

    def __exit__(self, *exc) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        if self._sftp:
            self._sftp.close()
        self._client.close()

    def forward(self, remote_host: str, remote_port: int) -> int:
        """Forward a free local port to remote_host:remote_port as seen from the VPS."""
        server = _ForwardServer(("127.0.0.1", 0), _ForwardHandler)
        server.transport = self._client.get_transport()  # type: ignore[assignment]
        server.remote = (remote_host, remote_port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._servers.append(server)
        return server.server_address[1]

    def sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self._sftp

    def run(self, command: str, timeout: int = 90) -> str:
        _, stdout, _ = self._client.exec_command(command, timeout=timeout)
        return stdout.read().decode("utf-8", "replace")
