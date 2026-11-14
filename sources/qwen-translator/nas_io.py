"""Explicit path mapping, byte identities and journalled sidecar publication.

All functions run in disposable worker processes, never on the Qt/UI thread.
The source video is only ever opened with 'rb'. No NAS directories are created.
"""
import ctypes
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid

from nas_api import digest
from video_subtitles import decode, parse_srt


class Waiting(OSError):
    pass


class AtomicRenameUnsupported(Waiting):
    def __init__(self, number):
        super().__init__(number, '当前挂载不支持防覆盖重命名，无法安全写回字幕；请改用 SMB/CIFS 挂载并重新测试。')


class Conflict(Exception):
    pass


class VersionChanged(Exception):
    pass


def file_hash(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(path):
    path = Path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or not before.st_size:
        raise Waiting('媒体文件为空或不是普通文件，等待导入完成。')
    sha = file_hash(path)
    after = path.stat()
    fields = ('st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_ino', 'st_dev')
    if any(getattr(before, k) != getattr(after, k) for k in fields):
        raise Waiting('媒体读取期间仍在变化，稍后重试。')
    return {'sha256': sha, 'size': after.st_size, 'mtime_ns': after.st_mtime_ns}


def mount_identity(path):
    """Include the mounted source, not an ephemeral mount id or ro/rw flags."""
    def unescape(value):
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
    found = None
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        a, b = line.split(' - ', 1)
        fields, rest = a.split(), b.split()
        root = unescape(fields[4])
        if rest[0] == 'autofs':
            continue
        if Path(path).is_relative_to(root) and (found is None or len(root) > len(found['root'])):
            found = {'root': root, 'type': rest[0], 'source': unescape(rest[1]),
                     'fsroot': unescape(fields[3])}
    if found is None:
        raise Waiting('无法确认本机挂载身份。')
    return found


def mapping_root(mapping, *, checking=False):
    remote = PurePosixPath(mapping.get('remote', ''))
    local = Path(mapping.get('local', '')).expanduser()
    if not remote.is_absolute() or '..' in remote.parts or not local.is_absolute():
        raise Waiting('请填写绝对远端前缀与本机目录，并先运行路径测试。')
    local = local.resolve(strict=True)
    if not local.is_dir():
        raise Waiting('本机映射目录不可用。')
    mounted = mount_identity(local)
    if mapping.get('require_mount', True) and (mounted['root'] == '/' or not mounted['type'].startswith(
            ('fuse', 'nfs', 'cifs', 'smb', '9p', 'ceph', 'afs'))):
        raise Waiting('指定路径目前没有 NAS 挂载，已停止以免写入本机空目录。')
    if not checking:
        proof = mapping.get('proof') or {}
        if (proof.get('local') != str(local) or proof.get('remote') != str(remote)
                or proof.get('mount') != mounted):
            raise Waiting('路径尚未验证，或挂载来源已变化；请重新执行路径测试。')
    return remote, local, mounted


def resolve_media(remote_path, mappings, kind):
    remote = PurePosixPath(remote_path)
    if not remote.is_absolute() or '..' in remote.parts:
        raise Waiting('媒体路径包含无效目录。')
    candidates = [m for m in mappings if m.get('kind') == kind and m.get('remote')
                  and remote.is_relative_to(PurePosixPath(m['remote']))]
    if not candidates:
        raise Waiting('没有匹配此媒体路径的映射；请配置并测试远端前缀。')
    mapping = max(candidates, key=lambda m: len(PurePosixPath(m['remote']).parts))
    prefix, local, _ = mapping_root(mapping)
    target = (local / str(remote.relative_to(prefix))).resolve(strict=True)
    if not target.is_relative_to(local) or not target.is_file():
        raise Waiting('媒体路径越出映射目录或文件不可读。')
    return target


def rename_noreplace(source, target):
    """Kernel no-clobber rename. Do not fall back to copy or check-then-rename."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, 'renameat2', None)
    if function is None:
        raise Waiting('此系统不支持无覆盖原子重命名。')
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        number = ctypes.get_errno()
        if number in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            raise AtomicRenameUnsupported(number)
        raise OSError(number, os.strerror(number))


def flush_file(f):
    f.flush()
    try:
        os.fsync(f.fileno())
    except OSError as error:
        # Some FUSE servers do not expose fsync. Closed-file readback is still required.
        if error.errno not in {errno.EINVAL, errno.ENOTSUP, errno.ENOSYS}:
            raise


def write_probe(parent):
    """Test temporary creation, readback, no-clobber rename and atomic replacement."""
    parent = Path(parent)
    paths = [parent / ('.qwen-nas-probe-' + uuid.uuid4().hex + suffix) for suffix in ('.tmp', '.check')]
    a, b = paths
    try:
        body = os.urandom(4096)
        with a.open('xb') as f:
            f.write(body)
            flush_file(f)
        if a.read_bytes() != body:
            raise Waiting('挂载临时文件回读不一致。')
        rename_noreplace(a, b)
        with a.open('xb') as f:
            f.write(b'new')
            flush_file(f)
        try:
            rename_noreplace(a, b)
        except FileExistsError:
            pass
        else:
            raise Waiting('挂载未保留同名文件，禁止自动写回。')
        if b.read_bytes() != body:
            raise Waiting('无覆盖重命名测试失败。')
        os.replace(a, b)
        if b.read_bytes() != b'new':
            raise Waiting('重命名后的内容回读不一致。')
    finally:
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def probe_mapping(mapping):
    prefix, local, mounted = mapping_root(mapping, checking=True)
    with os.scandir(local) as entries:
        sample = [entry.name for _, entry in zip(range(8), entries)]
    result = {**mapping, 'proof': {'local': str(local), 'remote': str(prefix), 'mount': mounted},
              'read_ok': True, 'write_ok': False, 'sample': sample}
    try:
        write_probe(local)
        result.update(write_ok=True, message='目录可读；临时文件、回读、无覆盖重命名和替换测试通过。')
    except OSError as error:
        if isinstance(error, AtomicRenameUnsupported):
            reason = error.strerror
        elif error.errno == errno.EROFS:
            reason = '当前进程看到的挂载为只读；请检查挂载选项及运行环境的写入限制。'
        elif isinstance(error, PermissionError):
            reason = '当前账号没有目录写入权限；请检查共享权限和目录权限。'
        elif isinstance(error, Waiting):
            reason = str(error)
        else:
            reason = '写回测试失败；请检查 NAS 连接、剩余空间及挂载状态。'
        result['write_error'] = {'errno': error.errno, 'reason': reason}
        result['message'] = '目录可读；' + reason + '生成结果将保留在本机。'
    return result


def valid_srt(path):
    body = Path(path).read_text(encoding='utf-8')
    cues = parse_srt(body)
    blocks = re.split(r'\n\s*\n', body.strip())
    ids = [b.splitlines()[0].strip() for b in blocks]
    if len(cues) != len(blocks) or ids != [str(i) for i in range(1, len(cues) + 1)]:
        raise ValueError('字幕编号、有效条数或时间戳校验失败。')
    return cues


def managed_stale(queue, path, video):
    record = queue.db.execute('SELECT * FROM outputs WHERE path=?', (str(path),)).fetchone()
    if record:
        # Even externally edited old managed subtitles need explicit human choice.
        return record['video'] != video
    old = queue.db.execute('SELECT video FROM sources WHERE path=? AND hash=?',
                           (str(path), file_hash(path))).fetchone()
    return old is not None and old['video'] != video


def remember_source(queue, path, video):
    with queue.db:
        queue.db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?)', (str(path), file_hash(path), video))


def staging_paths(target, job_id, sha):
    prefix = '.qwen-nas-' + digest(str(target))[:16] + '-' + job_id[:16] + '-' + sha[:16]
    return target.with_name(prefix + '.tmp'), target.with_name(prefix + '.backup')


def recover_publication(queue, target, owned):
    """Recover a crash during a managed update without ever replacing a human file."""
    if not owned or owned['state'] != 'planned':
        return
    _, backup = staging_paths(target, owned['job'], owned['hash'])
    if not backup.exists():
        return
    if backup.is_symlink():
        raise Conflict('字幕更新备份被替换，需人工检查。')
    previous_ok = file_hash(backup) == owned['previous']
    if not target.exists():
        rename_noreplace(backup, target)  # Restore either the managed old output or an external edit.
        if not previous_ok:
            raise Conflict(f'{target.name} 在更新时被外部修改，原文件已恢复。')
    elif target.is_symlink() or not previous_ok or file_hash(target) != owned['hash']:
        raise Conflict(f'{target.name} 更新中遇到外部改动，当前文件与隐藏备份均已保留。')
    else:
        backup.unlink()  # Only verified old bytes of our own output.
        with queue.db:
            queue.db.execute("UPDATE outputs SET state='published',previous=NULL WHERE path=?", (str(target),))


def publish(queue, job, mappings):
    result = job['result']
    if 'bilingual' not in result['outputs']:
        raise VersionChanged('本机结果缺少最终中英双语 SRT，需要补充英文并重新生成。')
    source = resolve_media(job['data']['path'], mappings, job['service'].split(':', 1)[0])
    if str(source) != result['source'] or fingerprint(source) != result['fingerprint']:
        raise VersionChanged('视频内容或版本已变化，禁止回写旧字幕；请同步并重新处理。')
    def signature():
        s = source.stat()
        return (s.st_dev, s.st_ino, s.st_ctime_ns, s.st_mtime_ns, s.st_size)
    source_signature = signature()
    plans = []
    for suffix, artifact in result['outputs'].items():
        if suffix not in {'zh', 'bilingual'}:
            raise ValueError('输出字幕类型无效。')
        local = Path(artifact['path'])
        cues = valid_srt(local)
        if suffix == 'bilingual' and any(len(c.text.splitlines()) < 2 for c in cues):
            raise VersionChanged('双语成品校验失败：存在未配对条目，需要重新生成。')
        if file_hash(local) != artifact['hash']:
            raise ValueError('本机生成结果已改变，请重新生成。')
        target = source.with_name(source.stem + '.' + suffix + '.srt')
        owned = queue.db.execute('SELECT * FROM outputs WHERE path=?', (str(target),)).fetchone()
        recover_publication(queue, target, owned)
        owned = queue.db.execute('SELECT * FROM outputs WHERE path=?', (str(target),)).fetchone()
        current = None
        try:
            target_stat = target.lstat()
        except FileNotFoundError:
            target_stat = None
        if target_stat:
            if not stat.S_ISREG(target_stat.st_mode):
                raise Conflict(f'{target.name} 不是普通字幕文件，已保留。')
            current = file_hash(target)
            # A source subtitle reused verbatim is preserved, not adopted as our output.
            equivalent = False
            if not owned and result.get('reused_chinese'):
                try:
                    equivalent = parse_srt(decode(target.read_bytes())) == valid_srt(local)
                except (ValueError, UnicodeError):
                    pass
            if equivalent:
                plans.append((local, target, current, True))
                continue
            if not owned:
                raise Conflict(f'{target.name} 已存在且不属于本软件，已保留；本机字幕可在视频页导出。')
            if current == owned['hash'] and owned['video'] == result['video'] and current == artifact['hash']:
                with queue.db:
                    queue.db.execute("UPDATE outputs SET state='published',previous=NULL WHERE path=?", (str(target),))
                plans.append((local, target, current, True))
                continue  # Includes crash between rename and journal acknowledgement.
            expected = owned['previous'] if owned['state'] == 'planned' else owned['hash']
            if current != expected:
                raise Conflict(f'{target.name} 已被外部修改，禁止覆盖。')
            if not (job['override'] or {}).get('update_managed', job['config'].get('update_managed')):
                raise Conflict(f'{target.name} 是旧的受管输出；允许更新未被修改的受管字幕后重试。')
        plans.append((local, target, current, False))
    # Preflight every conflict before publishing either final filename.
    if any(not skip for _, _, _, skip in plans):
        write_probe(source.parent)
    for local, target, previous, skip in plans:
        latest = queue.job(job['id'])
        if (not latest['present'] or latest['data']['version'] != job['version']
                or latest['state'] in {'superseded', 'missing', 'cancelled', 'paused'}):
            raise VersionChanged('任务对应的入库版本已失效，已停止回写。')
        sha = file_hash(local)
        if skip:
            continue
        temporary, backup = staging_paths(target, job['id'], sha)
        try:
            temporary.unlink(missing_ok=True)  # Only our deterministic staging name.
            with temporary.open('xb') as out, local.open('rb') as inp:
                out.write(inp.read())
                flush_file(out)
            if file_hash(temporary) != sha:
                raise Waiting('NAS 临时字幕回读不一致，成品已保留在本机。')
            if signature() != source_signature:
                raise VersionChanged('写回期间视频已变化，已停止发布旧字幕。')
            latest = queue.job(job['id'])
            if (not latest['present'] or latest['version'] != latest['data']['version']
                    or latest['state'] in {'superseded', 'missing', 'cancelled', 'paused'}):
                raise VersionChanged('写回前任务已暂停、取消或版本失效。')
            # Durable intent before the indivisible final-name rename.
            with queue.db:
                queue.db.execute('INSERT OR REPLACE INTO outputs VALUES (?,?,?,?,?,?)',
                    (str(target), result['video'], sha, previous, 'planned', job['id']))
            if previous is not None:
                if target.is_symlink() or file_hash(target) != previous:
                    raise Conflict(f'{target.name} 在写回前被修改，已保留。')
                # Move and then verify the exact old inode. A racing editor's bytes are
                # retained in the backup; no check-then-replace can overwrite them.
                rename_noreplace(target, backup)
                if backup.is_symlink() or file_hash(backup) != previous:
                    try:
                        rename_noreplace(backup, target)
                    except FileExistsError:
                        pass  # Keep both conflicting files for human inspection.
                    raise Conflict(f'{target.name} 更新期间被修改，已保留原文件 / 备份。')
            try:
                rename_noreplace(temporary, target)
            except FileExistsError:
                if previous is None:
                    # This collision proves we did not create the destination.
                    with queue.db:
                        queue.db.execute('DELETE FROM outputs WHERE path=? AND job=? AND state=?',
                                         (str(target), job['id'], 'planned'))
                raise Conflict(f'{target.name} 在写回时出现同名文件，已保留。') from None
            if file_hash(target) != sha:
                raise Waiting('NAS 成品回读校验失败，等待重新检查。')
            with queue.db:
                queue.db.execute("UPDATE outputs SET state='published',previous=NULL WHERE path=?", (str(target),))
            if previous is not None and backup.exists() and file_hash(backup) == previous:
                backup.unlink()
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return {'paths': [str(target) for _, target, _, _ in plans], 'library_verified': False}
