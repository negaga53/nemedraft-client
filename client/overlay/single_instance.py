"""Single-instance enforcement with raise-existing IPC.

Uses Qt's ``QLocalServer`` / ``QLocalSocket`` for cross-platform IPC.
The first instance becomes a listening server; subsequent launches
connect, send a ``RAISE`` command, and exit. The running instance
receives the command and surfaces its window.

Gated by the caller on ``sys.frozen`` so developers can launch multiple
overlays from source for side-by-side debugging.

Updater interaction
-------------------
The lock is bound to process lifetime. On Windows/macOS, the updater
spawns a helper that polls until the old PID exits before launching the
new binary — the listening socket dies with the old process. On Linux,
``os.execv`` replaces the process image; the stale socket file is
cleaned up via ``QLocalServer.removeServer`` on the next ``acquire``.
"""

from __future__ import annotations

import getpass
import hashlib
import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger("overlay.single_instance")

_RAISE_COMMAND = b"RAISE\n"
_CONNECT_TIMEOUT_MS = 200
_WRITE_TIMEOUT_MS = 200


class SingleInstance(QObject):
    """Cross-platform single-instance lock with raise-existing IPC."""

    raise_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server: QLocalServer | None = None
        self._socket_name: str | None = None

    @property
    def socket_name(self) -> str | None:
        """The resolved per-user socket name (after ``acquire`` runs)."""
        return self._socket_name

    def acquire(self, app_name: str) -> bool:
        """Try to become the singleton.

        Args:
            app_name: Logical app identifier; combined with a per-user
                hash to form the socket name.

        Returns:
            ``True`` if we are now the singleton (caller should proceed
            with normal startup). ``False`` if another instance was
            already running — in that case we've signalled it to raise
            its window and the caller should exit.
        """
        self._socket_name = _make_socket_name(app_name)

        if _notify_existing(self._socket_name):
            return False

        # No live instance — clear any stale socket file (Unix) and listen.
        QLocalServer.removeServer(self._socket_name)
        server = QLocalServer(self)
        if not server.listen(self._socket_name):
            logger.warning(
                "Could not start single-instance server (%s); proceeding without lock",
                server.errorString(),
            )
            # Fail open: never block the app because IPC plumbing broke.
            return True

        server.newConnection.connect(self._on_new_connection)
        self._server = server
        return True

    def _on_new_connection(self) -> None:
        assert self._server is not None
        sock = self._server.nextPendingConnection()
        if sock is None:
            return
        sock.readyRead.connect(self._on_ready_read)
        sock.disconnected.connect(sock.deleteLater)
        # If the client wrote-then-disconnected fast, ``readyRead`` may
        # have fired before our handler was wired up — drain whatever
        # is already buffered.
        self._drain(sock)

    def _on_ready_read(self) -> None:
        sender = self.sender()
        if isinstance(sender, QLocalSocket):
            self._drain(sender)

    def _drain(self, sock: QLocalSocket) -> None:
        data = bytes(sock.readAll())
        if _RAISE_COMMAND.strip() in data:
            self.raise_requested.emit()


def _make_socket_name(app_name: str) -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "anon"
    user_hash = hashlib.sha1(user.encode("utf-8")).hexdigest()[:8]
    return f"{app_name}-{user_hash}"


def _notify_existing(socket_name: str) -> bool:
    """Try to reach a running instance and tell it to raise.

    Returns ``True`` iff a running instance was reached. Blocks briefly
    on ``waitForDisconnected`` so the server (running in the *other*
    process) has time to read the RAISE command before our pipe end
    closes.
    """
    sock = QLocalSocket()
    sock.connectToServer(socket_name)
    if not sock.waitForConnected(_CONNECT_TIMEOUT_MS):
        return False

    sock.write(_RAISE_COMMAND)
    sock.flush()
    sock.waitForBytesWritten(_WRITE_TIMEOUT_MS)
    sock.disconnectFromServer()
    sock.waitForDisconnected(_WRITE_TIMEOUT_MS)
    return True
