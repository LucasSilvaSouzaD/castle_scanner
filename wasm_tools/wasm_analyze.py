#!/usr/bin/env python3
"""Offline WebAssembly analyzer for GOTWiC Scanner navigation recovery.

Standard library only. Works on the Unity .wasm binaries saved by wasm_diagnose.py.

Commands
  info FILE                          sections, function/table counts, SHA-256
  slot FILE SLOT [SLOT ...]          table slot -> function index, signature, body size
  func FILE INDEX [INDEX ...]        function index -> signature, body size, table slots
  pairs FILE [--near SLOT]           structural search for the nav/kingdom pair:
                                       slot S   : (i32,i32,i32,i32,i32) -> i32   (navigation)
                                       slot S-3 : (i64,i32,i32,i32,i32,i32) -> * (kingdom selector)
                                       kingdom index - nav index in 20..40
  fingerprint FILE --target nav=SLOT --target kingdom=SLOT -o REF.json
                                     save a small reference (bodies + features) of verified
                                     functions, so a future build can be matched without
                                     keeping the old binary around
  match NEW.wasm --ref REF.json      rank candidates in a new build against the reference

Function "name" in the browser (table.get(slot).name) is the WASM function index,
counting imported functions first. This tool uses the same numbering.
"""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import sys

VALTYPES = {0x7f: 'i32', 0x7e: 'i64', 0x7d: 'f32', 0x7c: 'f64', 0x7b: 'v128',
            0x70: 'funcref', 0x6f: 'externref'}
NAV_SIG = 'i32,i32,i32,i32,i32->i32'
KINGDOM_PARAMS = 'i64,i32,i32,i32,i32,i32'
PAIR_SLOT_OFFSET = -3          # kingdom slot = nav slot - 3 (all releases 2026-11-04..21)
PAIR_INDEX_DELTA = (20, 40)    # kingdom index - nav index (observed 27..31)


class WasmError(ValueError):
    pass


class Reader:
    __slots__ = ('data', 'pos', 'end')

    def __init__(self, data, pos=0, end=None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def u8(self):
        if self.pos >= self.end:
            raise WasmError('unexpected end of data')
        b = self.data[self.pos]
        self.pos += 1
        return b

    def u32(self):
        result = shift = 0
        while True:
            b = self.u8()
            result |= (b & 0x7f) << shift
            if b < 0x80:
                return result
            shift += 7
            if shift > 35:
                raise WasmError('LEB128 too long')

    def sleb(self, bits=64):
        result = shift = 0
        while True:
            b = self.u8()
            result |= (b & 0x7f) << shift
            shift += 7
            if b < 0x80:
                if b & 0x40:
                    result -= 1 << shift
                return result
            if shift > bits + 7:
                raise WasmError('signed LEB128 too long')

    def take(self, n):
        if self.pos + n > self.end:
            raise WasmError('unexpected end of data')
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def name(self):
        return self.take(self.u32()).decode('utf-8', 'replace')

    def valtype(self):
        b = self.u8()
        if b not in VALTYPES:
            raise WasmError(f'unsupported value type 0x{b:02x}')
        return VALTYPES[b]

    def limits(self):
        flags = self.u8()
        self.u32()
        if flags & 1:
            self.u32()


def const_expr(r):
    """Return int for i32.const / None for global.get or other initializers."""
    value = None
    op = r.u8()
    if op == 0x41:
        value = r.sleb(32)
    elif op == 0x42:
        r.sleb(64)
    elif op == 0x23:
        r.u32()
    elif op == 0xd2:
        value = ('ref.func', r.u32())
    elif op == 0xd0:
        r.u8()
    else:
        raise WasmError(f'unsupported constant expression opcode 0x{op:02x}')
    if r.u8() != 0x0b:
        raise WasmError('constant expression not terminated')
    return value


class Module:
    def __init__(self, data):
        if data[:4] != b'\0asm':
            raise WasmError('not a WebAssembly binary (bad magic)')
        if data[4:8] != b'\x01\0\0\0':
            raise WasmError('unsupported WebAssembly version')
        self.data = data
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.types = []
        self.import_func_types = []
        self.func_types = []
        self.bodies = []            # (start, end) of each defined function body
        self.table = {}             # slot -> function index (table 0)
        self.table_offset_unknown = False
        self.exports = {}
        self.names = {}
        self.sections = []
        r = Reader(data, 8)
        while r.pos < len(data):
            sid = r.u8()
            size = r.u32()
            start = r.pos
            end = start + size
            self.sections.append((sid, size))
            sr = Reader(data, start, end)
            if sid == 1:
                self._types(sr)
            elif sid == 2:
                self._imports(sr)
            elif sid == 3:
                self.func_types = [sr.u32() for _ in range(sr.u32())]
            elif sid == 7:
                self._exports(sr)
            elif sid == 9:
                self._elements(sr)
            elif sid == 10:
                self._code(sr)
            elif sid == 0:
                try:
                    if sr.name() == 'name':
                        self._names(sr)
                except WasmError:
                    pass
            r.pos = end
        if len(self.bodies) != len(self.func_types):
            raise WasmError('function and code section counts differ')
        self.slots_of = {}
        for slot, idx in self.table.items():
            self.slots_of.setdefault(idx, []).append(slot)

    # --- sections ---
    def _types(self, r):
        for _ in range(r.u32()):
            form = r.u8()
            if form != 0x60:
                raise WasmError(f'unsupported type form 0x{form:02x} (GC proposal?)')
            params = tuple(r.valtype() for _ in range(r.u32()))
            results = tuple(r.valtype() for _ in range(r.u32()))
            self.types.append((params, results))

    def _imports(self, r):
        for _ in range(r.u32()):
            r.name(); r.name()
            kind = r.u8()
            if kind == 0:
                self.import_func_types.append(r.u32())
            elif kind == 1:
                r.u8(); r.limits()
            elif kind == 2:
                r.limits()
            elif kind == 3:
                r.valtype(); r.u8()
            elif kind == 4:
                r.u8(); r.u32()
            else:
                raise WasmError(f'unsupported import kind {kind}')

    def _exports(self, r):
        for _ in range(r.u32()):
            name = r.name()
            kind = r.u8()
            idx = r.u32()
            if kind == 0:
                self.exports.setdefault(idx, []).append(name)

    def _elements(self, r):
        for _ in range(r.u32()):
            flags = r.u32()
            table = 0
            offset = None
            active = flags & 1 == 0
            if flags in (2, 6):
                table = r.u32()
            if active:
                offset = const_expr(r)
            if flags in (1, 2, 3):
                r.u8()                              # elemkind
            elif flags in (5, 6, 7):
                r.valtype()                         # reftype
            count = r.u32()
            if flags < 4:
                items = [r.u32() for _ in range(count)]
            else:
                items = []
                for _ in range(count):
                    v = const_expr(r)
                    items.append(v[1] if isinstance(v, tuple) else None)
            if not active or table != 0:
                continue
            if not isinstance(offset, int):
                self.table_offset_unknown = True
                offset = 0
            for i, idx in enumerate(items):
                if idx is not None:
                    self.table[offset + i] = idx

    def _code(self, r):
        for _ in range(r.u32()):
            size = r.u32()
            self.bodies.append((r.pos, r.pos + size))
            r.pos += size

    def _names(self, r):
        while r.pos < r.end:
            sub = r.u8()
            size = r.u32()
            end = r.pos + size
            if sub == 1:
                for _ in range(r.u32()):
                    idx = r.u32()
                    self.names[idx] = r.name()
            r.pos = end

    # --- queries ---
    @property
    def n_imports(self):
        return len(self.import_func_types)

    @property
    def n_functions(self):
        return self.n_imports + len(self.func_types)

    def sig(self, idx):
        if idx < self.n_imports:
            t = self.import_func_types[idx]
        else:
            t = self.func_types[idx - self.n_imports]
        return self.types[t]

    def sig_str(self, idx):
        params, results = self.sig(idx)
        return ','.join(params) + '->' + (','.join(results) or 'void')

    def body(self, idx):
        if idx < self.n_imports:
            return None
        start, end = self.bodies[idx - self.n_imports]
        return self.data[start:end]


# --- instruction decoding (MVP + bulk memory + sign-ext + sat-trunc + atomics) ---
_MEMARG = set(range(0x28, 0x3f))


def decode(module, body):
    """Return (normalized opcode tokens, called function indices, i32 constants)."""
    r = Reader(body)
    for _ in range(r.u32()):
        r.u32(); r.valtype()
    tokens, calls, consts = [], [], []
    while r.pos < r.end:
        op = r.u8()
        tok = op
        if op in (0x02, 0x03, 0x04):
            b = r.data[r.pos]
            if b == 0x40 or b in VALTYPES:
                r.pos += 1
            else:
                r.sleb(33)
        elif op in (0x0c, 0x0d):
            r.u32()
        elif op == 0x0e:
            for _ in range(r.u32() + 1):
                r.u32()
        elif op == 0x10:
            callee = r.u32()
            calls.append(callee)
        elif op == 0x11:
            r.u32(); r.u32()
        elif op == 0x1c:
            for _ in range(r.u32()):
                r.valtype()
        elif 0x20 <= op <= 0x26:
            r.u32()
        elif op in _MEMARG:
            r.u32(); r.u32()
        elif op in (0x3f, 0x40):
            r.u8()
        elif op == 0x41:
            v = r.sleb(32)
            consts.append(v)
        elif op == 0x42:
            r.sleb(64)
        elif op == 0x43:
            r.take(4)
        elif op == 0x44:
            r.take(8)
        elif op == 0xd0:
            r.u8()
        elif op == 0xd2:
            r.u32()
        elif op == 0xfc:
            sub = r.u32()
            tok = 0xfc00 + sub
            if sub == 8:
                r.u32(); r.u8()
            elif sub in (9, 13, 15, 16, 17):
                r.u32()
            elif sub == 10:
                r.u8(); r.u8()
            elif sub == 11:
                r.u8()
            elif sub in (12, 14):
                r.u32(); r.u32()
        elif op == 0xfe:
            sub = r.u32()
            tok = 0xfe00 + sub
            if sub == 0x03:
                r.u8()
            else:
                r.u32(); r.u32()
        elif op == 0xfd:
            tok = 0xfd00
            tokens.append(tok)
            break                       # SIMD: stop here, features stay comparable
        tokens.append(tok)
    return tokens, calls, consts


def features(module, idx):
    body = module.body(idx)
    if body is None:
        return None
    tokens, calls, consts = decode(module, body)
    return {
        'index': idx,
        'sig': module.sig_str(idx),
        'size': len(body),
        'tokens': tokens,
        'call_sigs': [module.sig_str(c) for c in calls],
        'consts': [c for c in consts if -4096 <= c <= 65535],
        'body_hex': body.hex(),
    }


def ratio(a, b):
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def score(ref, cand, raw=True):
    s_ops = ratio(ref['tokens'], cand['tokens'])
    s_calls = ratio(ref['call_sigs'], cand['call_sigs'])
    s_consts = ratio(ref['consts'], cand['consts'])
    s_size = min(ref['size'], cand['size']) / max(ref['size'], cand['size'])
    s_raw = ratio(bytes.fromhex(ref['body_hex']), bytes.fromhex(cand['body_hex'])) if raw else 0.0
    total = 0.40 * s_ops + 0.20 * s_calls + 0.10 * s_consts + 0.10 * s_size + 0.20 * s_raw
    return {'score': round(total, 4), 'opcodes': round(s_ops, 4), 'calls': round(s_calls, 4),
            'consts': round(s_consts, 4), 'size_ratio': round(s_size, 4), 'raw_bytes': round(s_raw, 4)}


def load(path):
    data = Path(path).read_bytes()
    return Module(data)


# --- commands ---
def cmd_info(a):
    m = load(a.file)
    names = {1: 'type', 2: 'import', 3: 'function', 4: 'table', 5: 'memory', 6: 'global',
             7: 'export', 8: 'start', 9: 'element', 10: 'code', 11: 'data', 12: 'datacount', 0: 'custom'}
    print(f'File            : {a.file}')
    print(f'Size / SHA-256  : {len(m.data)} bytes / {m.sha256}')
    print(f'Types           : {len(m.types)}')
    print(f'Functions       : {m.n_functions} ({m.n_imports} imported + {len(m.func_types)} defined)')
    print(f'Table 0 entries : {len(m.table)} (max slot {max(m.table) if m.table else "-"})'
          + ('  [offset from global.get - slots relative]' if m.table_offset_unknown else ''))
    print(f'Name section    : {"yes, " + str(len(m.names)) + " names" if m.names else "no"}')
    print('Sections        : ' + ', '.join(f'{names.get(s, s)}({n})' for s, n in m.sections))


def describe(m, idx):
    out = {'index': idx, 'sig': m.sig_str(idx), 'imported': idx < m.n_imports,
           'size': None if idx < m.n_imports else len(m.body(idx)),
           'slots': sorted(m.slots_of.get(idx, []))}
    if idx in m.names:
        out['name'] = m.names[idx]
    if idx in m.exports:
        out['exports'] = m.exports[idx]
    return out


def cmd_slot(a):
    m = load(a.file)
    for slot in a.slots:
        idx = m.table.get(slot)
        if idx is None:
            print(f'slot {slot}: empty / outside table')
        else:
            print(f'slot {slot}: ' + json.dumps(describe(m, idx)))


def cmd_func(a):
    m = load(a.file)
    for idx in a.indices:
        if not 0 <= idx < m.n_functions:
            print(f'function {idx}: out of range')
        else:
            print(f'function {idx}: ' + json.dumps(describe(m, idx)))


def find_pairs(m, near=None, min_size=200):
    pairs = []
    for slot, idx in m.table.items():
        if m.sig_str(idx) != NAV_SIG or len(m.body(idx) or b'') < min_size:
            continue
        k_slot = slot + PAIR_SLOT_OFFSET
        k_idx = m.table.get(k_slot)
        if k_idx is None:
            continue
        params, _ = m.sig(k_idx)
        if ','.join(params) != KINGDOM_PARAMS:
            continue
        delta = k_idx - idx
        if not PAIR_INDEX_DELTA[0] <= delta <= PAIR_INDEX_DELTA[1]:
            continue
        pairs.append({'nav_slot': slot, 'nav_index': idx, 'nav_size': len(m.body(idx) or b''),
                      'kingdom_slot': k_slot, 'kingdom_index': k_idx, 'kingdom_sig': m.sig_str(k_idx),
                      'index_delta': delta, 'distance': abs(slot - near) if near else None})
    pairs.sort(key=lambda p: (p['distance'] if p['distance'] is not None else 0, p['nav_slot']))
    return pairs


def cmd_pairs(a):
    m = load(a.file)
    pairs = find_pairs(m, a.near, a.min_size)
    if not pairs:
        print('No nav/kingdom pair matched the structural rule. Use "match" with a reference.')
        return 1
    for p in pairs[:a.top]:
        print(json.dumps(p))
    if len(pairs) == 1:
        p = pairs[0]
        print(f'\nUnique pair: nav slot {p["nav_slot"]} (function {p["nav_index"]}), '
              f'kingdom slot {p["kingdom_slot"]} (function {p["kingdom_index"]}).')
    return 0


def cmd_fingerprint(a):
    m = load(a.file)
    ref = {'format': 'gotwic-wasm-reference-v1', 'wasm_sha256': m.sha256, 'wasm_size': len(m.data),
           'source_file': Path(a.file).name, 'targets': {}}
    for spec in a.target:
        label, _, slot = spec.partition('=')
        slot = int(slot)
        idx = m.table.get(slot)
        if idx is None:
            raise SystemExit(f'slot {slot} is empty in {a.file}')
        f = features(m, idx)
        f['slot'] = slot
        ref['targets'][label] = f
        print(f'{label}: slot {slot} -> function {idx} {f["sig"]} size {f["size"]}')
    if a.note:
        ref['note'] = a.note
    Path(a.output).write_text(json.dumps(ref, indent=1), encoding='utf-8')
    print(f'Reference written: {a.output}')


def rank(m, ref_f, top, table_only=True, size_window=(0.5, 2.0)):
    cands = []
    lo, hi = ref_f['size'] * size_window[0], ref_f['size'] * size_window[1]
    pool = m.slots_of.keys() if table_only else range(m.n_imports, m.n_functions)
    for idx in pool:
        if idx < m.n_imports or m.sig_str(idx) != ref_f['sig']:
            continue
        size = len(m.body(idx))
        if not lo <= size <= hi:
            continue
        f = features(m, idx)
        cands.append((score(ref_f, f, raw=False)['score'], idx, f))
    cands.sort(key=lambda c: -c[0])
    out = []
    for _, idx, f in cands[:max(top * 4, 40)]:
        s = score(ref_f, f, raw=True)
        out.append({'index': idx, 'slots': sorted(m.slots_of.get(idx, [])), 'size': f['size'], **s})
    out.sort(key=lambda c: -c['score'])
    return out[:top], len(cands)


def cmd_match(a):
    ref = json.loads(Path(a.ref).read_text(encoding='utf-8'))
    m = load(a.file)
    print(f'New build: {a.file}  sha256 {m.sha256[:16]}…  ({m.n_functions} functions, {len(m.table)} table slots)')
    print(f'Reference: {a.ref}  (build sha256 {ref.get("wasm_sha256", "?")[:16]}…)\n')
    results = {}
    for label, ref_f in ref['targets'].items():
        top, pool = rank(m, ref_f, a.top, table_only=not a.all_functions)
        results[label] = top
        print(f'== {label}: reference slot {ref_f["slot"]} function {ref_f["index"]} '
              f'{ref_f["sig"]} size {ref_f["size"]}  ({pool} same-signature candidates)')
        for c in top:
            print(f'   function {c["index"]:>7} slots {c["slots"]}  size {c["size"]:>5}  score {c["score"]:.3f}  '
                  f'(ops {c["opcodes"]:.2f} calls {c["calls"]:.2f} consts {c["consts"]:.2f} '
                  f'size {c["size_ratio"]:.2f} raw {c["raw_bytes"]:.2f})')
        print()
    # Cross-check: targets must keep their relative slot distance (nav vs kingdom = -3).
    labels = list(ref['targets'])
    if len(labels) >= 2:
        base = labels[0]
        base_slot = ref['targets'][base]['slot']
        best = None
        for c0 in results[base][:5]:
            for s0 in c0['slots']:
                total = c0['score']
                chosen = {base: (s0, c0)}
                ok = True
                for other in labels[1:]:
                    want = s0 + (ref['targets'][other]['slot'] - base_slot)
                    hit = next((c for c in results[other] if want in c['slots']), None)
                    if hit is None:
                        ok = False
                        break
                    total += hit['score']
                    chosen[other] = (want, hit)
                if ok and (best is None or total > best[0]):
                    best = (total, chosen)
        if best:
            print('Consistent combination (same relative slot distance as the reference):')
            for label, (slot, c) in best[1].items():
                print(f'   {label:<8} slot {slot}  function {c["index"]}  score {c["score"]:.3f}')
            print('\nNext: validate ONE navigation call live before applying:')
            nav = best[1].get('nav')
            if nav:
                print(f'   python wasm_tools/wasm_diagnose.py test --slot {nav[0]} --name {nav[1]["index"]}')
        else:
            print('No combination keeps the reference slot distance. Do not apply blindly; '
                  'compare the top candidates manually.')
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('info'); s.add_argument('file'); s.set_defaults(fn=cmd_info)
    s = sub.add_parser('slot'); s.add_argument('file'); s.add_argument('slots', type=int, nargs='+'); s.set_defaults(fn=cmd_slot)
    s = sub.add_parser('func'); s.add_argument('file'); s.add_argument('indices', type=int, nargs='+'); s.set_defaults(fn=cmd_func)
    s = sub.add_parser('pairs'); s.add_argument('file'); s.add_argument('--near', type=int)
    s.add_argument('--min-size', type=int, default=200, help='minimum nav body size in bytes (verified builds: ~575)')
    s.add_argument('--top', type=int, default=15); s.set_defaults(fn=cmd_pairs)
    s = sub.add_parser('fingerprint'); s.add_argument('file')
    s.add_argument('--target', action='append', required=True, help='label=SLOT, e.g. nav=63922')
    s.add_argument('-o', '--output', required=True); s.add_argument('--note'); s.set_defaults(fn=cmd_fingerprint)
    s = sub.add_parser('match'); s.add_argument('file'); s.add_argument('--ref', required=True)
    s.add_argument('--top', type=int, default=8)
    s.add_argument('--all-functions', action='store_true', help='also consider functions not in the table')
    s.set_defaults(fn=cmd_match)
    a = p.parse_args()
    try:
        return a.fn(a) or 0
    except (WasmError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
