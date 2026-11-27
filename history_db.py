"""Scan history (SQLite) and target signals for the Targets tab.

Every completed kingdom scan is stored as one row per castle (player, position, status, power,
level, title, alliance, kills, merit). From that history we derive what one scan cannot show:
  * power change since the previous scan where the player's power was known
  * how long a castle has been unprotected (consecutive scans without a bubble)
  * whether the castle moved
The per-player summary ('latest') is rebuilt only for the players of the scan being saved, so the
Targets query is a plain indexed SELECT however long the history gets.
Old CSV snapshots are imported once (in the background). CSV files, MySQL and the upload are untouched.
"""
import csv
import datetime as dt
from pathlib import Path
import re
import sqlite3

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / 'data' / 'history.db'
CSV_NAME = re.compile(r'^\d+_\d{8}_\d{6}_\d{6}(?:_migrated_\d+)?\.csv$')

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY, source TEXT UNIQUE, kingdom INTEGER, seen TEXT, castles INTEGER);
CREATE TABLE IF NOT EXISTS obs (
    scan_id INTEGER, pkey TEXT, kingdom INTEGER, player_id TEXT, name TEXT, x INTEGER, y INTEGER,
    status TEXT, power REAL, level INTEGER, peerage INTEGER, alliance TEXT, kills INTEGER, merit INTEGER,
    seen TEXT);
CREATE INDEX IF NOT EXISTS obs_player ON obs(pkey, seen);
CREATE INDEX IF NOT EXISTS obs_scan ON obs(scan_id);
CREATE INDEX IF NOT EXISTS obs_seen ON obs(seen);
CREATE TABLE IF NOT EXISTS latest (
    pkey TEXT PRIMARY KEY, kingdom INTEGER, player_id TEXT, name TEXT, x INTEGER, y INTEGER, status TEXT,
    level INTEGER, peerage INTEGER, alliance TEXT, seen TEXT, power REAL, power_seen TEXT, prev_power REAL,
    kills INTEGER, merit INTEGER, unprot_since TEXT, unprot_scans INTEGER, moved INTEGER, n_obs INTEGER);
CREATE INDEX IF NOT EXISTS latest_filter ON latest(status, kingdom, seen);
CREATE INDEX IF NOT EXISTS latest_power ON latest(power);
"""
OBS_COLS = 'scan_id, pkey, kingdom, player_id, name, x, y, status, power, level, peerage, alliance, kills, merit, seen'


def _pkey(player_id, kingdom, name):
    pid = str(player_id or '').strip()
    return pid if pid and pid != '0' else f'k{kingdom}:{name}'


def _float(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _seen_text(value):
    """CSV/report timestamps ('2026-11-25 21:53:47.111' or ISO) -> sortable 'YYYY-MM-DD HH:MM:SS' (UTC)."""
    s = str(value or '').strip().replace('T', ' ').rstrip('Z')
    return s[:19] if len(s) >= 19 else s


def _summarise(pkey, rows):
    """rows: obs of one player, oldest first -> one 'latest' row with the derived signals."""
    last = rows[-1]
    (_, _, kingdom, player_id, name, x, y, status, _, _, _, _, _, _, seen) = last

    def latest_value(i):
        for r in reversed(rows):
            if r[i] is not None:
                return r
        return None
    p = latest_value(8)
    power, power_seen, prev_power = (p[8], p[14], None) if p else (None, None, None)
    if p:
        idx = rows.index(p)
        for r in reversed(rows[:idx]):
            if r[8] is not None:
                prev_power = r[8]
                break
    unprot_since, unprot_scans = None, 0
    if status == 'UNPROTECTED':
        for r in reversed(rows):
            if r[7] != 'UNPROTECTED':
                break
            unprot_since, unprot_scans = r[14], unprot_scans + 1
    moved = int(len(rows) > 1 and (rows[-2][5], rows[-2][6]) != (x, y))
    pick = lambda i: (latest_value(i) or [None] * 15)[i]
    return (pkey, kingdom, player_id, name, x, y, status, pick(9), pick(10), pick(11), seen,
            power, power_seen, prev_power, pick(12), pick(13), unprot_since, unprot_scans, moved, len(rows))


class History:
    def __init__(self, path=DB_PATH):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.last_total = 0

    def close(self):
        self.db.close()

    # ------------------------------------------------------------------ writing
    def has_source(self, source):
        return self.db.execute('SELECT 1 FROM scans WHERE source=?', (str(source),)).fetchone() is not None

    def _refresh_latest(self, pkeys):
        pkeys = list(dict.fromkeys(pkeys))
        cur = self.db.cursor()
        for i in range(0, len(pkeys), 500):
            chunk = pkeys[i:i + 500]
            marks = ','.join('?' * len(chunk))
            by_player = {}
            for r in cur.execute(f'SELECT {OBS_COLS} FROM obs WHERE pkey IN ({marks}) ORDER BY pkey, seen, scan_id', chunk):
                by_player.setdefault(r[1], []).append(r)
            cur.execute(f'DELETE FROM latest WHERE pkey IN ({marks})', chunk)
            cur.executemany('INSERT INTO latest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            [_summarise(pk, rows) for pk, rows in by_player.items()])

    def record_scan(self, source, kingdom, seen, castles):
        """castles: dicts with x, y, name, player_id, status, power_millions, level, peerage, alliance, kills, merit."""
        source = str(source)
        seen = _seen_text(seen)
        cur = self.db.cursor()
        old = cur.execute('SELECT id FROM scans WHERE source=?', (source,)).fetchone()
        stale = []
        if old:  # re-recording the same snapshot replaces it
            stale = [r[0] for r in cur.execute('SELECT DISTINCT pkey FROM obs WHERE scan_id=?', (old[0],))]
            cur.execute('DELETE FROM obs WHERE scan_id=?', (old[0],))
            cur.execute('DELETE FROM scans WHERE id=?', (old[0],))
        cur.execute('INSERT INTO scans(source, kingdom, seen, castles) VALUES (?,?,?,?)',
                    (source, _int(kingdom), seen, len(castles)))
        scan_id = cur.lastrowid
        rows = []
        for c in castles:
            k = _int(c.get('kingdom', kingdom))
            name = str(c.get('name') or '')
            rows.append((scan_id, _pkey(c.get('player_id'), k, name), k, str(c.get('player_id') or ''), name,
                         _int(c.get('x')), _int(c.get('y')), str(c.get('status') or '').upper(),
                         _float(c.get('power_millions', c.get('power'))), _int(c.get('level')),
                         _int(c.get('peerage')), (c.get('alliance') or None), _int(c.get('kills')),
                         _int(c.get('merit')), seen))
        cur.executemany(f'INSERT INTO obs ({OBS_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        self._refresh_latest([r[1] for r in rows] + stale)
        self.db.commit()
        return scan_id

    def update_power(self, source, kingdom, x, y, name, power_millions, kills=None, merit=None):
        """A power lookup after the scan: update that castle in that snapshot."""
        row = self.db.execute('SELECT id FROM scans WHERE source=?', (str(source),)).fetchone()
        if not row:
            return 0
        sets, args = ['power=?'], [_float(power_millions)]
        if kills is not None:
            sets.append('kills=?'); args.append(_int(kills))
        if merit is not None:
            sets.append('merit=?'); args.append(_int(merit))
        where = 'scan_id=? AND kingdom=? AND x=? AND y=? AND name=?'
        key = (row[0], _int(kingdom), _int(x), _int(y), str(name))
        pkeys = [r[0] for r in self.db.execute(f'SELECT pkey FROM obs WHERE {where}', key)]
        n = self.db.execute(f'UPDATE obs SET {", ".join(sets)} WHERE {where}', (*args, *key)).rowcount
        self._refresh_latest(pkeys)
        self.db.commit()
        return n

    def import_csv(self, path):
        """Import one scanner CSV snapshot (once). Returns number of castles imported."""
        path = Path(path)
        if self.has_source(path.name):
            return 0
        with path.open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return 0
        castles = [{'kingdom': r.get('kingdom'), 'x': r.get('x'), 'y': r.get('y'), 'name': r.get('name'),
                    'player_id': r.get('player_id'), 'status': r.get('status'),
                    'power_millions': r.get('status_source')} for r in rows]
        self.record_scan(path.name, rows[0].get('kingdom'), rows[0].get('last_seen_utc'), castles)
        return len(castles)

    def prune(self, keep_days):
        """Forget observations older than keep_days (0 = keep everything)."""
        if not keep_days or keep_days <= 0:
            return 0
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)).strftime('%Y-%m-%d %H:%M:%S')
        stale = [r[0] for r in self.db.execute('SELECT DISTINCT pkey FROM obs WHERE seen < ?', (cutoff,))]
        if not stale:
            return 0
        n = self.db.execute('DELETE FROM obs WHERE seen < ?', (cutoff,)).rowcount
        self.db.execute('DELETE FROM scans WHERE seen < ?', (cutoff,))
        self._refresh_latest(stale)
        self.db.execute('DELETE FROM latest WHERE pkey NOT IN (SELECT DISTINCT pkey FROM obs)')
        self.db.commit()
        return n

    # ------------------------------------------------------------------ reading
    def kingdoms(self):
        return [r[0] for r in self.db.execute('SELECT DISTINCT kingdom FROM latest WHERE kingdom IS NOT NULL ORDER BY kingdom')]

    def counts(self):
        s = self.db.execute('SELECT COUNT(*), MIN(seen), MAX(seen) FROM scans').fetchone()
        o = self.db.execute('SELECT COUNT(*) FROM latest').fetchone()
        return {'scans': s[0], 'first': s[1], 'last': s[2], 'players': o[0]}

    def targets(self, statuses=('UNPROTECTED',), min_power=0.0, min_level=0, kingdom=None,
                exclude_alliances=(), search='', seen_within_hours=None, limit=5000):
        """Latest state of every player matching the filters (strongest first), with signals."""
        where, args = [], []
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})"); args += [s.upper() for s in statuses]
        if kingdom is not None:
            where.append('kingdom = ?'); args.append(int(kingdom))
        if min_level:
            where.append('COALESCE(level, 0) >= ?'); args.append(int(min_level))
        if min_power:
            where.append('COALESCE(power, 0) >= ?'); args.append(float(min_power))
        excl = [a.strip().casefold() for a in exclude_alliances if a and a.strip()]
        if excl:
            where.append(f"(alliance IS NULL OR lower(alliance) NOT IN ({','.join('?' * len(excl))}))"); args += excl
        if search:
            where.append("(lower(name) LIKE ? OR lower(COALESCE(alliance, '')) LIKE ?)")
            args += [f'%{search.casefold()}%'] * 2
        if seen_within_hours:
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=seen_within_hours)).strftime('%Y-%m-%d %H:%M:%S')
            where.append('seen >= ?'); args.append(cutoff)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        self.last_total = self.db.execute(f'SELECT COUNT(*) FROM latest {clause}', args).fetchone()[0]
        sql = (f"SELECT pkey, kingdom, player_id, name, x, y, status, level, peerage, alliance, seen, power, "
               f"power_seen, prev_power, kills, merit, unprot_since, unprot_scans, moved, n_obs FROM latest "
               f"{clause} ORDER BY COALESCE(power, 0) DESC LIMIT ?")
        out = []
        for (pkey, k, pid, name, x, y, status, level, peerage, alliance, seen, power, power_seen, prev_power,
             kills, merit, unprot_since, unprot_scans, moved, n_obs) in self.db.execute(sql, (*args, int(limit))):
            out.append({'pkey': pkey, 'kingdom': k, 'player_id': pid, 'name': name, 'x': x, 'y': y,
                        'status': status, 'level': level, 'peerage': peerage, 'alliance': alliance, 'seen': seen,
                        'power': power or 0.0, 'power_seen': power_seen,
                        'power_delta': (power - prev_power) if power and prev_power else None,
                        'kills': kills, 'merit': merit, 'unprotected_since': unprot_since,
                        'unprotected_scans': unprot_scans or 0, 'moved': bool(moved), 'observations': n_obs})
        return out


def import_folder(folder, db_path=DB_PATH, keep_days=0):
    """Import scanner CSVs that are not in the history yet and prune old data (runs in a worker thread)."""
    h = History(db_path)
    try:
        h.prune(keep_days)
        n_files = n_castles = 0
        # oldest first (file names are <kingdom>_<UTC timestamp>), so each player's history builds in order
        for path in sorted(Path(folder).glob('*.csv'), key=lambda p: p.name.split('_', 1)[-1]):
            if not CSV_NAME.match(path.name):
                continue
            try:
                c = h.import_csv(path)
            except (OSError, UnicodeError, csv.Error, sqlite3.Error):
                continue
            if c:
                n_files += 1; n_castles += c
        return n_files, n_castles
    finally:
        h.close()
