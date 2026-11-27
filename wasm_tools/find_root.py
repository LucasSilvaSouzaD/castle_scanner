#!/usr/bin/env python3
"""Procura caminhos fixos na memória do jogo que levam a um valor conhecido (ex.: o gerenciador
da classificação), para reencontrá-lo depois que a página é recarregada.

Só lê a memória. A varredura é feita em pedaços para não travar o jogo.

  search   (com os valores da sessão ATUAL) acha cadeias  G -> [+o2] -> [+o1] -> valor,
           onde G está na área estática (endereço fixo do binário). Salva ranking/rank_roots.json
  resolve  (depois de recarregar) segue as cadeias salvas e mostra o que cada uma dá agora.
"""
import argparse
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from wasm_diagnose import CDP, BUILDS  # noqa: E402
import wasm_analyze as wa  # noqa: E402
from rank_request import JS_MEM, DEFAULTS  # noqa: E402

ROOTS_FILE = ROOT / 'ranking' / 'rank_roots.json'
CHUNK = 24 * 1024 * 1024        # u32 entries per evaluate (96 MB)
MAX_OFF = 0x100


def static_end(module):
    """Initial value of the first mutable i32 global (Emscripten stack pointer = top of static+stack)."""
    r = wa.Reader(module.data, 8)
    while r.pos < len(module.data):
        sid = r.u8(); size = r.u32(); end = r.pos + size
        if sid == 6:
            for _ in range(r.u32()):
                vt = r.valtype(); mut = r.u8()
                v = wa.const_expr(r)
                if vt == 'i32' and mut and isinstance(v, int):
                    return v
        r.pos = end
    raise SystemExit('Não achei o ponteiro de pilha inicial no binário.')


SCAN_JS = JS_MEM + """
if(!__mem)return {error:'memória indisponível'};
const T=new Set(TARGETS);const out=[];const s=START,e=Math.min(END,__mem.length);
for(let i=s;i<e;i++){const v=__mem[i];if(T.has(v)){out.push([i*4,v]);if(out.length>=CAP)break;}}
return {done:e,len:__mem.length,hits:out};"""


def scan(cdp, targets, start_addr=0, end_addr=None, cap=20000, label=''):
    targets = sorted(set(int(t) for t in targets if t > 0))
    hits = []
    first = start_addr // 4
    info = cdp.evaluate('(()=>{' + JS_MEM + 'return {len:__mem?__mem.length:0};})()')
    total = info['len'] if end_addr is None else min(info['len'], end_addr // 4)
    pos = first
    while pos < total and len(hits) < cap:
        js = (SCAN_JS.replace('TARGETS', json.dumps(targets)).replace('START', str(pos))
              .replace('END', str(min(total, pos + CHUNK))).replace('CAP', str(cap - len(hits))))
        res = cdp.evaluate('(()=>{' + js + '})()', timeout=60)
        if 'error' in res:
            raise SystemExit(res['error'])
        hits.extend(res['hits'])
        pos = min(total, pos + CHUNK)
        print(f'  {label} {pos * 4 / 1048576:.0f}/{total * 4 / 1048576:.0f} MB, {len(hits)} achados', end='\r', flush=True)
        time.sleep(0.05)
    print()
    return hits


def read(cdp, addr):
    js = JS_MEM + f'return __mem&&({addr}>>>2)<__mem.length?__mem[{addr}>>>2]:null;'
    return cdp.evaluate('(()=>{' + js + '})()')


def chains_to(cdp, value, sstart, send, depth):
    """Static roots G with mem[...mem[mem[G]+o2]...+o1] == value, up to `depth` dereferences."""
    found = []
    p1 = [a for a, _ in scan(cdp, [value], label='nível 1')]
    found += [{'root': a, 'offsets': []} for a in p1 if sstart <= a < send]
    if depth < 2:
        return found
    b1 = {}
    for a in p1[:3000]:
        for o in range(0, MAX_OFF, 4):
            b1.setdefault(a - o, []).append(o)
    p2 = scan(cdp, list(b1), label='nível 2')
    for a, v in p2:
        if sstart <= a < send:
            for o1 in b1[v]:
                found.append({'root': a, 'offsets': [o1]})
    if depth < 3:
        return found
    b2 = {}
    for a, v in p2[:6000]:
        if sstart <= a < send:
            continue
        for o in range(0, MAX_OFF, 4):
            b2.setdefault(a - o, []).append((o, v))
    p3 = scan(cdp, list(b2), sstart, send, label='nível 3 (estático)')
    for a, v in p3:
        for o2, v2 in b2[v]:
            for o1 in b1[v2]:
                found.append({'root': a, 'offsets': [o2, o1]})
    return found


def follow(cdp, chain):
    v = read(cdp, chain['root'])
    for o in chain['offsets']:
        if not v:
            return None
        v = read(cdp, v + o)
    return v


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', type=int, default=9222)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('search')
    s.add_argument('--this', dest='this_ptr', type=int, default=DEFAULTS['this'])
    s.add_argument('--method', type=int, default=DEFAULTS['method'])
    sub.add_parser('resolve')
    p.add_argument('--static-end', type=int, help='fim da área estática (padrão: lido do binário)')
    a = p.parse_args()

    builds = sorted(BUILDS.glob('*.wasm'), key=lambda f: f.stat().st_mtime, reverse=True)
    module = wa.load(builds[0])
    send = a.static_end or static_end(module)
    sstart = 1024
    cdp = CDP(a.port)
    try:
        cdp.attach()
        if a.cmd == 'search':
            print(f'Área estática: {sstart}..{send} ({send / 1048576:.1f} MB)')
            klass = read(cdp, a.this_ptr)
            print(f'Objeto {a.this_ptr} klass {klass}')
            print('\n[1/2] Caminhos até o objeto da classificação:')
            this_chains = chains_to(cdp, a.this_ptr, sstart, send, depth=3)
            print('[2/2] Caminhos até o parâmetro "method":')
            method_chains = chains_to(cdp, a.method, sstart, send, depth=2)
            for c in this_chains:
                c['root_is_klass'] = read(cdp, c['root']) == klass
            this_chains.sort(key=lambda c: (not c['root_is_klass'], len(c['offsets']), sum(c['offsets']), c['root']))
            method_chains.sort(key=lambda c: (len(c['offsets']), c['root']))
            data = {'build': builds[0].name, 'session': {'this': a.this_ptr, 'klass': klass, 'method': a.method},
                    'this_chains': this_chains[:200], 'method_chains': method_chains[:200]}
            ROOTS_FILE.parent.mkdir(exist_ok=True)
            ROOTS_FILE.write_text(json.dumps(data, indent=1), encoding='utf-8')
            print(f'\nCaminhos até o objeto: {len(this_chains)} | até o method: {len(method_chains)}')
            for c in this_chains[:8]:
                print('  objeto:', c)
            for c in method_chains[:5]:
                print('  method:', c)
            print(f'Salvo em {ROOTS_FILE.relative_to(ROOT)}')
        else:
            data = json.loads(ROOTS_FILE.read_text(encoding='utf-8'))
            print('Sessão anterior:', data['session'])
            for key in ('this_chains', 'method_chains'):
                results = {}
                for c in data[key]:
                    results.setdefault(follow(cdp, c), []).append(c)
                print(f'\n{key}: resultados agora (valor -> nº de caminhos que dão esse valor)')
                for v, cs in sorted(results.items(), key=lambda kv: -len(kv[1]))[:8]:
                    print(f'  {v}: {len(cs)} caminho(s), ex.: {cs[0]}')
    finally:
        cdp.close()


if __name__ == '__main__':
    main()
