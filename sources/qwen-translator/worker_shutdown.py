"""Bounded Qt worker shutdown on Linux, including independently grouped helpers."""
import os
from pathlib import Path
import signal


STOP_GRACE_MS = 5000
KILL_GRACE_MS = 1000


def _identity(pid):
    try:
        # comm may contain spaces or parentheses; fields after it start at state.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _same_process(pid, identity):
    current = _identity(pid)
    return current is not None and current[1] == identity[1]


def _descendants(pid):
    if pid <= 1 or (root_identity := _identity(pid)) is None:
        return []
    parents = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdecimal() and (identity := _identity(int(entry.name))):
            parents.setdefault(identity[0], []).append((int(entry.name), identity))
    result = [(pid, root_identity)]
    for parent, _ in result:
        result.extend(child for child in parents.get(parent, []) if child not in result)
    return result


class WorkerTree:
    """Capture only this worker's descendants; keep identity after reparenting."""
    def __init__(self, pid):
        self.members = []
        for child, identity in _descendants(pid):
            if not _same_process(child, identity):
                continue
            fd = None
            if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
                try:
                    fd = os.pidfd_open(child)
                except OSError:
                    pass
            if _same_process(child, identity):
                self.members.append((child, identity, fd))
            elif fd is not None:
                os.close(fd)

    def send(self, sig):
        for pid, identity, fd in reversed(self.members):
            try:
                if fd is not None:
                    signal.pidfd_send_signal(fd, sig)
                elif _same_process(pid, identity):
                    # Compatibility for kernels without pidfd support.
                    os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def close(self):
        for _, _, fd in self.members:
            if fd is not None:
                os.close(fd)
        self.members.clear()


def stop_worker(process, is_current):
    """Request normal cleanup, then terminate/kill the captured worker tree."""
    from PyQt6.QtCore import QProcess, QTimer

    process.write(b'stop\n')
    process.closeWriteChannel()

    def terminate():
        if not is_current() or process.state() == QProcess.ProcessState.NotRunning:
            return
        tree = WorkerTree(int(process.processId()))
        def kill(*_):
            try:
                tree.send(signal.SIGKILL)
            finally:
                tree.close()

        # The window may close as soon as finished fires. Do not leave cleanup
        # dependent on an event loop that can stop before the timer expires.
        process.finished.connect(kill)
        tree.send(signal.SIGTERM)
        # Keep the captured identities even if QProcess finishes and is deleted.
        QTimer.singleShot(KILL_GRACE_MS, kill)

    QTimer.singleShot(STOP_GRACE_MS, terminate)
