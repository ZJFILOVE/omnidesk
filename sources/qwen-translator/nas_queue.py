"""Local SQLite inventory, durable queue, provenance and publication journal."""
import json
from pathlib import Path
import sqlite3
import time

from nas_api import base_url, digest, service_id

CONFIG_KEYS = ('text_provider', 'text_endpoint', 'text_model', 'api_format', 'codex_model',
               'codex_reasoning_effort', 'antigravity_model', 'translation_concurrency',
               'asr_backend', 'sync_mode', 'outputs', 'search_provider', 'update_managed')
ACTIVE = ('queued', 'running', 'waiting', 'generated', 'writing', 'waiting_write')
LABELS = {'listed': '仅入库清单', 'queued': '等待处理', 'running': '处理中',
          'waiting': '等待读取 / 网络', 'generated': '字幕已生成', 'writing': '正在写回 NAS',
          'waiting_write': '等待写回', 'written': '已写回 NAS · 未验证加载',
          'written_partial': '旧单语结果 · 需补充双语', 'paused': '已暂停', 'cancelled': '已取消',
          'failed': '处理失败', 'conflict': '同名字幕冲突', 'manual': '待人工选择字幕',
          'superseded': '文件版本已更新', 'missing': '服务已无此文件'}


def processing_config(config):
    result = {k: config[k] for k in CONFIG_KEYS if k in config}
    if result.get('text_endpoint'):
        result['text_endpoint'] = base_url(result['text_endpoint'])
    result.setdefault('outputs', ['zh', 'bilingual'])
    if not result['outputs'] or set(result['outputs']) - {'zh', 'bilingual'}:
        raise ValueError('至少选择中文或双语字幕输出。')
    # The NAS deliverable is always bilingual; Chinese-only is an additional export.
    result['outputs'] = [s for s in ('zh', 'bilingual') if s in result['outputs'] or s == 'bilingual']
    result.setdefault('sync_mode', 'none')
    result.setdefault('search_provider', 'none')
    result.setdefault('update_managed', False)
    return result


class Queue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS services (id TEXT PRIMARY KEY, synced REAL, error TEXT);
          CREATE TABLE IF NOT EXISTS media (id TEXT PRIMARY KEY, service TEXT, file_id TEXT,
              version TEXT, data TEXT, present INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, media_id TEXT, version TEXT,
              config TEXT, state TEXT, stage TEXT DEFAULT '', progress REAL DEFAULT 0,
              error TEXT DEFAULT '', origin TEXT DEFAULT '', result TEXT, next_try REAL DEFAULT 0,
              override TEXT DEFAULT '{}', created REAL, updated REAL);
          CREATE TABLE IF NOT EXISTS outputs (path TEXT PRIMARY KEY, video TEXT, hash TEXT,
              previous TEXT, state TEXT, job TEXT);
          CREATE TABLE IF NOT EXISTS sources (path TEXT, hash TEXT, video TEXT,
              PRIMARY KEY(path,hash));
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        ''')

    def close(self):
        self.db.close()

    def recover(self):
        with self.db:
            self.db.execute("UPDATE jobs SET state=CASE WHEN result IS NULL THEN 'queued' ELSE 'waiting_write' END, "
                            "stage='应用重启，继续检查点', next_try=0 WHERE state IN ('running','writing')")

    def setting(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))
            return value
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def sync(self, service, items, config, auto=False):
        sid = service_id(service)
        first = self.db.execute('SELECT synced FROM services WHERE id=?', (sid,)).fetchone() is None
        # Validate a complete snapshot before touching presence flags or first-sync state.
        unique = {str(item['file_id']): item for item in items}
        for item in unique.values():
            if not item.get('version') or not item.get('path'):
                raise ValueError('不完整的入库清单。')
        with self.db:
            self.db.execute('UPDATE media SET present=0 WHERE service=?', (sid,))
            for item in unique.values():
                mid = digest([sid, item['file_id']])
                old = self.db.execute('SELECT version FROM media WHERE id=?', (mid,)).fetchone()
                changed = old is None or old['version'] != item['version']
                if changed and old:
                    self.db.execute("UPDATE jobs SET state='superseded', error='媒体文件版本已变化', updated=? "
                                    "WHERE media_id=? AND state!='cancelled'", (time.time(), mid))
                self.db.execute('INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,1)',
                    (mid, sid, item['file_id'], item['version'], json.dumps(item, ensure_ascii=False)))
                if auto and not first and changed:
                    self._enqueue(mid, item['version'], config)
            # Failure never reaches this transaction. Only a fully successful response marks absence.
            self.db.execute("UPDATE jobs SET state='missing', error='完整同步后服务已无此文件' "
                            "WHERE media_id IN (SELECT id FROM media WHERE service=? AND present=0) "
                            "AND state IN ('queued','running','writing','waiting','generated','waiting_write')", (sid,))
            self.db.execute('INSERT OR REPLACE INTO services VALUES (?,?,?)', (sid, time.time(), ''))
        return {'first': first, 'count': len(unique)}

    def sync_error(self, service, message):
        # Do not create a baseline record on failure.
        with self.db:
            self.db.execute('UPDATE services SET error=? WHERE id=?', (message, service_id(service)))

    def _enqueue(self, mid, version, config):
        config = processing_config(config)
        jid = digest([mid, version, config])
        now = time.time()
        self.db.execute('INSERT OR IGNORE INTO jobs(id,media_id,version,config,state,created,updated) '
                        'VALUES (?,?,?,?,?,?,?)', (jid, mid, version, json.dumps(config), 'queued', now, now))
        return jid

    def enqueue(self, ids, config):
        result = []
        with self.db:
            for mid in ids:
                media = self.db.execute('SELECT * FROM media WHERE id=? AND present=1', (mid,)).fetchone()
                if media:
                    result.append(self._enqueue(mid, media['version'], config))
        return result

    def job(self, jid):
        row = self.db.execute('SELECT j.*,m.service,m.data,m.present FROM jobs j JOIN media m '
                              'ON m.id=j.media_id WHERE j.id=?', (jid,)).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ('data', 'config', 'result', 'override'):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def update(self, jid, **values):
        allowed = {'state', 'stage', 'progress', 'error', 'origin', 'result', 'next_try', 'override'}
        if set(values) - allowed:
            raise ValueError('未知队列字段。')
        for k in ('result', 'override'):
            if k in values and values[k] is not None:
                values[k] = json.dumps(values[k], ensure_ascii=False)
        values['updated'] = time.time()
        with self.db:
            self.db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                            (*values.values(), jid))

    def next_job(self, services=None):
        allowed = ''
        params = [time.time()]
        if services is not None:
            if not services:
                return None
            allowed = ' AND m.service IN (' + ','.join('?' for _ in services) + ')'
            params.extend(services)
        row = self.db.execute("SELECT j.id FROM jobs j JOIN media m ON m.id=j.media_id "
            "WHERE j.state IN ('queued','waiting','generated','waiting_write') AND j.next_try<=? "
            "AND m.present=1 AND m.version=j.version" + allowed + " ORDER BY j.created LIMIT 1", params).fetchone()
        return self.job(row['id']) if row else None

    def retry(self, jid, override=None):
        job = self.job(jid)
        if not job or not job['present'] or job['version'] != job['data']['version']:
            return
        # Completed/published jobs are not silently reset by periodic synchronisation.
        if job['state'] in {'running', 'writing', 'superseded', 'missing'}:
            return
        values = dict(state='waiting_write' if job['result'] else 'queued', next_try=0, error='')
        if override is not None:
            values.update(override=override, result=None, state='queued')
        self.update(jid, **values)

    def snapshot(self):
        entries = []
        for row in self.db.execute('SELECT * FROM media ORDER BY service, id'):
            media = dict(row)
            media['data'] = json.loads(media['data'])
            jobs = self.db.execute('SELECT * FROM jobs WHERE media_id=? ORDER BY updated DESC', (row['id'],))
            media['jobs'] = []
            for j in jobs:
                item = dict(j)
                for key in ('config', 'result', 'override'):
                    item[key] = json.loads(item[key]) if item[key] else None
                media['jobs'].append(item)
            entries.append(media)
        return entries
