"""File pickers that do not automatically browse a network mount."""
import os
from pathlib import Path
import re

from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QFileDialog


def initial_directory(value):
    """Inspect mount metadata, never stat/resolve the potentially offline path."""
    value = os.path.abspath(os.path.expanduser(str(value or Path.home())))
    remote_roots = ['/mnt', '/media', '/net', '/run/user']
    try:
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields, filesystem = line.split(' - ', 1)
            kind = filesystem.split()[0]
            if kind.startswith(('fuse', 'nfs', 'cifs', 'smb', 'autofs', '9p', 'ceph', 'afs')):
                mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields.split()[4])
                remote_roots.append(mount)
    except OSError:
        pass
    if any(value == root or value.startswith(root.rstrip('/') + '/') for root in remote_roots):
        return str(Path.home())
    return value


def choose_file(parent, title, initial, name_filter, *, save=False, suffix='srt'):
    """Use the Qt list picker without desktop icon/portal integration."""
    dialog = QFileDialog(parent)
    dialog.setOptions(QFileDialog.Option.DontUseNativeDialog
                      | QFileDialog.Option.DontUseCustomDirectoryIcons
                      | QFileDialog.Option.DontResolveSymlinks)
    dialog.setWindowTitle(title)
    dialog.setNameFilter(name_filter)
    dialog.setViewMode(QFileDialog.ViewMode.List)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave if save else QFileDialog.AcceptMode.AcceptOpen)
    dialog.setFileMode(QFileDialog.FileMode.AnyFile if save else QFileDialog.FileMode.ExistingFile)
    home = str(Path.home())
    # Qt can restore old sidebar/history entries pointing at sleeping mounts.
    dialog.setSidebarUrls([QUrl.fromLocalFile(home)])
    dialog.setHistory([home])
    suggested = Path(initial) if save else None
    dialog.setDirectory(initial_directory(suggested.parent if save else initial))
    if save:
        dialog.setDefaultSuffix(suffix)
        dialog.selectFile(suggested.name)
    try:
        if dialog.exec() == QFileDialog.DialogCode.Accepted:
            files = dialog.selectedFiles()
            return files[0] if files else ''
        return ''
    finally:
        dialog.deleteLater()
