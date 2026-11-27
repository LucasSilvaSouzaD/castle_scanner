"""Player power from the in-game ranking (JSON protocol, game build of 2026-11-25).

When the ranking window is opened in the game, the server sends
  20401 worldRankTopResponse   (top-1 of each ranking)
  20403 rankDetailInfoResponse (full list, e.g. the 100 strongest of the kingdom)
Every player entry carries id, name, allianceCode and current power. The scanner
and the power detector record these frames whenever they see them, so the power of
ranked players is known without visiting their castle.

Cache: ranking/power_ranking.json (newest value per player id wins).
"""
import json
import os
from pathlib import Path
import time

BASE = Path(__file__).resolve().parent
RANK_FILE = BASE / 'ranking' / 'power_ranking.json'
RANK_MESSAGE_IDS = (20401, 20403)
DEFAULT_MAX_AGE_HOURS = float(os.environ.get('GOTWIC_RANK_MAX_AGE_HOURS', '6'))


def frame_json(raw):
    """Decode a game frame [len][id][uncompressed size][reserved][payload] whose payload is JSON."""
    if len(raw) < 14:
        return None
    size = int.from_bytes(raw[8:12], 'big')
    payload = raw[13:]
    if size:
        import lz4.block
        payload = lz4.block.decompress(payload, uncompressed_size=size)
    payload = payload.lstrip()
    if payload[:1] != b'{':
        return None
    return json.loads(payload.decode('utf-8'))


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _player_entries(node):
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, dict):
            # Player rows have peerageScore; alliance rows do not (their power is the alliance total).
            if 'id' in cur and 'name' in cur and 'power' in cur and 'peerageScore' in cur:
                yield cur
            else:
                stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))


def entries_from_frame(raw):
    mid = int.from_bytes(raw[4:8], 'big') if len(raw) >= 8 else None
    if mid not in RANK_MESSAGE_IDS:
        return []
    doc = frame_json(raw)
    if not doc:
        return []
    now = time.time()
    out = []
    for e in _player_entries(doc):
        try:
            power = int(e['power'])
            pid = str(int(e['id']))
        except (TypeError, ValueError, KeyError):
            continue
        name = str(e.get('name') or '')
        if power < 0 or not name:
            continue
        out.append({'id': pid, 'name': name, 'power': power,
                    'alliance': str(e.get('allianceCode') or '') or None,
                    'world_id': str(e.get('world_Id') or '') or None,
                    'rank': e.get('rank'), 'seen': now,
                    'kill': _int_or_none(e.get('kill')), 'merit': _int_or_none(e.get('peerageScore'))})
    return out


def _read():
    try:
        data = json.loads(RANK_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) and isinstance(data.get('players'), dict) else {'players': {}}
    except (OSError, ValueError):
        return {'players': {}}


def record(entries):
    """Merge ranking entries into the cache. Returns the number of entries stored."""
    if not entries:
        return 0
    data = _read()
    players = data['players']
    for e in entries:
        old = players.get(e['id'])
        if old is None or old.get('seen', 0) <= e['seen']:
            players[e['id']] = e
    data['updated'] = time.time()
    RANK_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RANK_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, RANK_FILE)
    return len(entries)


def record_frame(raw):
    try:
        return record(entries_from_frame(raw))
    except Exception:
        return 0


class Ranking:
    """Read-only view of fresh cache entries, indexed by player id and by exact name."""

    def __init__(self, max_age_hours=DEFAULT_MAX_AGE_HOURS):
        limit = time.time() - max_age_hours * 3600
        players = [p for p in _read()['players'].values() if p.get('seen', 0) >= limit]
        self.by_id = {p['id']: p for p in players}
        names = {}
        for p in players:
            names.setdefault(p['name'], []).append(p)
        # Ambiguous names (two players, same nickname) are never used for matching.
        self.by_name = {n: v[0] for n, v in names.items() if len(v) == 1}

    def __len__(self):
        return len(self.by_id)

    def find(self, player_id=None, name=None):
        if player_id not in (None, ''):
            try:
                hit = self.by_id.get(str(int(player_id)))
            except (TypeError, ValueError):
                hit = None
            if hit:
                return hit if not name or hit['name'] == name else None
        if name:
            return self.by_name.get(name)
        return None
