#!/usr/bin/env python3
"""Lê os argumentos com que o jogo chama funções WASM específicas (ex.: pedido da classificação).

Coloca um breakpoint temporário no início de cada função, lê os parâmetros quando o jogo
a chama, e continua a execução na hora (a pausa dura milissegundos). No final remove os
breakpoints e desliga o depurador. Não altera nada no jogo.

Uso (Chrome de depuração aberto, logado, mapa na tela):
    .venv-mac/bin/python wasm_tools/capture_args.py --seconds 40
    (padrão: funções 62397 e 62398 — pedidos 20401/20403 da classificação)
Durante o tempo, abra Classificação > Classificação Local > Classificação de Poder, feche
e abra de novo.
"""
import argparse
import datetime as dt
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


def first_instruction_offset(module, idx):
    start, end = module.bodies[idx - module.n_imports]
    r = wa.Reader(module.data, start, end)
    for _ in range(r.u32()):
        r.u32(); r.valtype()
    return r.pos


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', type=int, default=9222)
    p.add_argument('--seconds', type=float, default=40)
    p.add_argument('--max-hits', type=int, default=4, help='leituras por função')
    p.add_argument('functions', type=int, nargs='*', default=[62397, 62398])
    a = p.parse_args()

    builds = sorted(BUILDS.glob('*.wasm'), key=lambda f: f.stat().st_mtime, reverse=True) if BUILDS.exists() else []
    if not builds:
        raise SystemExit('Nenhum binário em wasm_builds/. Rode antes: wasm_diagnose.py dump')
    module = wa.load(builds[0])
    offsets = {}
    for idx in a.functions:
        if not module.n_imports <= idx < module.n_functions:
            raise SystemExit(f'Função {idx} fora do binário {builds[0].name}')
        offsets[idx] = first_instruction_offset(module, idx)
        print(f'função {idx}: {module.sig_str(idx)}  offset 0x{offsets[idx]:x}')

    cdp = CDP(a.port)
    scripts, paused = [], []
    orig_poll = cdp.poll

    def poll():
        try:
            m = json.loads(cdp.ws.recv())
        except cdp._ws_mod.WebSocketTimeoutException:
            return
        if m.get('id') is not None:
            cdp.replies[m['id']] = m
            return
        method, params = m.get('method'), m.get('params', {})
        if method == 'Runtime.executionContextCreated':
            cdp.contexts.append(params.get('context', {}))
        elif method == 'Debugger.scriptParsed':
            if params.get('scriptLanguage') == 'WebAssembly' or str(params.get('url', '')).startswith('wasm://'):
                scripts.append(params)
        elif method == 'Debugger.paused':
            paused.append(params)
    cdp.poll = poll

    hits = {idx: [] for idx in a.functions}
    bp_to_func = {}
    try:
        cdp.attach()
        cdp.wait(cdp.send('Debugger.enable', {}), 60)
        cdp.collect(1.0)
        if not scripts:
            raise SystemExit('Módulo WASM não apareceu no depurador.')
        size = len(module.data)
        script = next((s for s in scripts if s.get('endColumn') == size), None) or \
            max(scripts, key=lambda s: s.get('endColumn', 0))
        print(f'Módulo no depurador: {script.get("url")} (tamanho {script.get("endColumn")}, binário {size})')
        for idx, off in offsets.items():
            r = cdp.wait(cdp.send('Debugger.setBreakpoint', {'location': {
                'scriptId': script['scriptId'], 'lineNumber': 0, 'columnNumber': off}}), 30)
            if 'error' in r:
                raise SystemExit(f'Breakpoint na função {idx} falhou: {r["error"]}')
            bp_to_func[r['result']['breakpointId']] = idx
        print(f'\nGravando {a.seconds:g}s — abra Classificação > Classificação Local > Classificação de Poder '
              '(feche e abra de novo)...', flush=True)
        end = time.monotonic() + a.seconds
        while time.monotonic() < end:
            cdp.poll()
            while paused:
                ev = paused.pop(0)
                frame = (ev.get('callFrames') or [{}])[0]
                idx = next((bp_to_func[b] for b in ev.get('hitBreakpoints', []) if b in bp_to_func), None)
                values = {}
                try:
                    if idx is not None and len(hits[idx]) < a.max_hits:
                        for scope in frame.get('scopeChain', []):
                            if scope.get('type') != 'local':
                                continue
                            props = cdp.wait(cdp.send('Runtime.getProperties', {
                                'objectId': scope['object']['objectId'], 'ownProperties': True}), 5)
                            for prop in props.get('result', {}).get('result', []):
                                v = prop.get('value', {})
                                val = v.get('value', v.get('description'))
                                if v.get('subtype') == 'wasmvalue' and v.get('objectId'):
                                    inner = cdp.wait(cdp.send('Runtime.getProperties', {
                                        'objectId': v['objectId'], 'ownProperties': True}), 5)
                                    for ip in inner.get('result', {}).get('result', []):
                                        if ip.get('name') == 'value':
                                            iv = ip.get('value', {})
                                            val = iv.get('value', iv.get('unserializableValue', iv.get('description')))
                                values[prop['name']] = val
                        hits[idx].append({'function': frame.get('functionName'), 'locals': values})
                        print(f'  função {idx} chamada com: {values}', flush=True)
                finally:
                    cdp.wait(cdp.send('Debugger.resume'), 5)
    finally:
        for bp in bp_to_func:
            try:
                cdp.wait(cdp.send('Debugger.removeBreakpoint', {'breakpointId': bp}), 5)
            except Exception:
                pass
        try:
            cdp.wait(cdp.send('Debugger.disable'), 10)
        except Exception:
            pass
        cdp.poll = orig_poll
        cdp.close()

    out_dir = HERE / 'captures'
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f'args_{dt.datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    out.write_text(json.dumps({'build': builds[0].name, 'hits': hits}, indent=1, ensure_ascii=False), encoding='utf-8')
    print('\nResumo:')
    for idx, lst in hits.items():
        print(f'  função {idx}: {len(lst)} chamada(s)')
        for h in lst:
            print('    ', h['locals'])
    print(f'Arquivo: {out.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
