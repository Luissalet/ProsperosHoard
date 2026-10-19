"""Persistent relationships between native studio objects, never format conversion."""
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid


class Projects:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()

    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.root / 'studio-projects.sqlite3')
        conn.row_factory = sqlite3.Row
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,title TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY,project TEXT NOT NULL,module TEXT NOT NULL,
                kind TEXT NOT NULL,native_id TEXT NOT NULL,revision TEXT NOT NULL,route TEXT NOT NULL,
                snapshot TEXT NOT NULL,created REAL NOT NULL,UNIQUE(project,module,kind,native_id,revision));
            CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY,project TEXT NOT NULL,
                source TEXT NOT NULL,target TEXT NOT NULL,receipt TEXT NOT NULL,created REAL NOT NULL);
        ''')
        return conn

    def run(self, operation):
        with self.lock:
            conn = self._connect()
            try:
                return operation(conn)
            finally:
                conn.commit()
                conn.close()

    def list(self):
        # Discovery does not create a store.
        if not (self.root / 'studio-projects.sqlite3').is_file():
            return []
        return self.run(lambda c: [dict(r) for r in c.execute('SELECT * FROM projects ORDER BY created DESC')])

    def create(self, title):
        title = str(title).strip()
        if not title:
            raise ValueError('Give the project a title.')
        item = dict(id=uuid.uuid4().hex, title=title[:200], created=time.time())
        self.run(lambda c: c.execute('INSERT INTO projects VALUES(:id,:title,:created)', item))
        return self.get(item['id'])

    def get(self, project):
        def read(c):
            row = c.execute('SELECT * FROM projects WHERE id=?', (project,)).fetchone()
            if row is None:
                raise ValueError('Unknown studio project.')
            return {**dict(row), 'objects': [dict(r) | {'snapshot': json.loads(r['snapshot'])}
                for r in c.execute('SELECT * FROM objects WHERE project=? ORDER BY created', (project,))],
                'transfers': [dict(r) | {'receipt': json.loads(r['receipt'])}
                for r in c.execute('SELECT * FROM transfers WHERE project=? ORDER BY created', (project,))]}
        return self.run(read)

    def link(self, project, module, kind, native_id, route, snapshot):
        self.get(project)
        raw = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        revision = hashlib.sha256(raw.encode()).hexdigest()
        item = dict(id=uuid.uuid4().hex, project=project, module=module, kind=kind, native_id=native_id,
                    revision=revision, route=route, snapshot=raw, created=time.time())
        def write(c):
            c.execute('INSERT OR IGNORE INTO objects VALUES(:id,:project,:module,:kind,:native_id,:revision,:route,:snapshot,:created)', item)
            return dict(c.execute('SELECT * FROM objects WHERE project=? AND module=? AND kind=? AND native_id=? AND revision=?',
                (project, module, kind, native_id, revision)).fetchone())
        return self.run(write)

    def receipt(self, project, source, target, receipt):
        item = dict(id=uuid.uuid4().hex, project=project, source=source, target=target,
                    receipt=json.dumps(receipt, ensure_ascii=False), created=time.time())
        self.run(lambda c: c.execute('INSERT INTO transfers VALUES(:id,:project,:source,:target,:receipt,:created)', item))
        return self.get(project)

    def project_for_timeline(self, timeline_id):
        if not (self.root / 'studio-projects.sqlite3').exists():
            return None
        def read(c):
            row = c.execute("SELECT project FROM objects WHERE module='generation' AND kind='timeline' AND native_id=? ORDER BY created DESC LIMIT 1", (timeline_id,)).fetchone()
            return row['project'] if row else None
        return self.run(read)
