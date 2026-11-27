#!/usr/bin/env python3
"""Captura os frames WebSocket do jogo para diagnosticar o decodificador de castelos (9001).

Uso (Chrome de depuração aberto, logado, com o mapa na tela):
    .venv-mac/bin/python wasm_tools/capture_frames.py                 # 15 s, você arrasta o mapa com o mouse
    .venv-mac/bin/python wasm_tools/capture_frames.py --x 300 --y 300 # também navega 1x via WASM

Grava wasm_tools/captures/frames_<hora>.jsonl e mostra um resumo:
quantos frames de cada tipo, tamanhos, LZ4, e quantos castelos o decodificador atual achou.
"""
import argparse
import base64
import collections
import datetime as dt
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from wasm_diagnose import CDP, JS_TABLE, read_expected  # noqa: E402
from live_truce_scanner_v5 import frame, full_9001_records, json_castles  # noqa: E402
from extract_coordinates_from_protocol import objects  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', type=int, default=9222)
    p.add_argument('--seconds', type=float, default=15)
    p.add_argument('--x', type=int)
    p.add_argument('--y', type=int)
    a = p.parse_args()

    out_dir = HERE / 'captures'
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f'frames_{dt.datetime.now().strftime("%Y%m%d_%H%M%S")}.jsonl'
    cdp = CDP(a.port)
    raw_frames = []
    original_poll = cdp.poll

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
        elif method in ('Network.webSocketFrameReceived', 'Network.webSocketFrameSent'):
            resp = params.get('response', {})
            if resp.get('opcode') == 2:
                raw_frames.append({'t': round(time.monotonic(), 3),
                                   'dir': 'recv' if method.endswith('Received') else 'sent',
                                   'payload_base64': resp.get('payloadData', '')})
    cdp.poll = poll
    try:
        cdp.attach(network=True)
        if a.x is not None and a.y is not None:
            exp = read_expected()
            js = JS_TABLE + f"""const f=t.get({exp['nav']['slot']});
if(!f||String(f.name)!=='{exp['nav']['name']}')return {{ok:false,error:'identidade'}};
return {{ok:true,returnValue:String(f({a.y * 720 + a.x},150,200,250,300))}};"""
            print('Navegando via WASM para', a.x, a.y, '->', cdp.evaluate('(()=>{' + js + '})()'))
        else:
            print(f'Capturando {a.seconds:g}s — ARRASTE O MAPA com o mouse por uma área com castelos...')
        cdp.collect(a.seconds)
    finally:
        cdp.poll = original_poll
        cdp.close()

    counts = collections.Counter()
    sizes = collections.defaultdict(list)
    full_total, compact_total, lz4_errors = 0, 0, []
    names = []
    with out.open('w', encoding='utf-8') as fh:
        for fr in raw_frames:
            raw = base64.b64decode(fr['payload_base64'])
            mid = int.from_bytes(raw[4:8], 'big') if len(raw) >= 8 else -1
            fr['message_id'] = mid
            fr['length'] = len(raw)
            fh.write(json.dumps(fr) + '\n')
            counts[(fr['dir'], mid)] += 1
            sizes[(fr['dir'], mid)].append(len(raw))
            if fr['dir'] == 'recv' and mid == 9001:
                try:
                    parsed = frame({'kind': 'Network.webSocketFrameReceived',
                                    'params': {'response': {'opcode': 2, 'payloadData': fr['payload_base64']}}})
                except Exception as exc:
                    lz4_errors.append(str(exc))
                    continue
                if not parsed:
                    continue
                _, body = parsed
                full = list(full_9001_records(body)) + list(json_castles(body[13:]))
                compact = list(objects(body))
                full_total += len(full)
                compact_total += len(compact)
                names.extend(n for _, _, n, _ in full[:3])
    import power_ranking
    ranked = sum(power_ranking.record_frame(base64.b64decode(fr['payload_base64']))
                 for fr in raw_frames if fr['dir'] == 'recv' and fr['message_id'] in power_ranking.RANK_MESSAGE_IDS)
    if ranked:
        print(f'Classificação: {ranked} jogadores salvos em ranking/power_ranking.json')
    print(f'\nArquivo: {out.relative_to(ROOT)}  ({len(raw_frames)} frames)')
    print('Frames por (direção, id): tamanho médio')
    for key, n in counts.most_common(25):
        s = sizes[key]
        print(f'  {key[0]:4} {key[1]:>7}: {n:>5}x  média {sum(s) // len(s):>7} bytes  máx {max(s)}')
    print(f'\n9001: registros completos {full_total}, compactos {compact_total}, erros LZ4 {len(lz4_errors)}')
    if names:
        print('Exemplos de nomes:', ', '.join(names[:8]))
    print('\nMande este resumo (ou me avise) para eu analisar o arquivo.')


if __name__ == '__main__':
    main()
