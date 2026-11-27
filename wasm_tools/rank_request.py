#!/usr/bin/env python3
"""Pede a Classificação de Poder ao jogo, sem abrir a janela (função WASM 62398 / slot 76143).

Comandos
  probe    Lê (sem chamar nada) o objeto gerenciador da classificação na memória do jogo.
  call     UMA chamada controlada; espera a resposta 20403 e salva os jogadores no cache
           ranking/power_ranking.json (o mesmo que o scanner usa).

Os valores vêm de capture_args.py (25/11/2026):
  62398(this=145194912, 1, 1, method=150764104)
"""
import argparse
import base64
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from wasm_diagnose import CDP, JS_TABLE  # noqa: E402
import power_ranking  # noqa: E402

DEFAULTS = {'slot': 76143, 'name': '62398', 'this': 145194912, 'a1': 1, 'a2': 1, 'method': 150764104}

JS_MEM = JS_TABLE + """
const __mem=__M?.HEAPU32||(__M?.wasmMemory?new Uint32Array(__M.wasmMemory.buffer):null)
  ||(__M?.asm?.memory?new Uint32Array(__M.asm.memory.buffer):null);"""


def words(cdp, address, count=8):
    js = JS_MEM + f"""
if(!__mem)return {{ok:false,error:'memória indisponível'}};
const a={address}>>>2;if(a+{count}>__mem.length)return {{ok:false,error:'endereço fora da memória'}};
return {{ok:true,size:__mem.length*4,words:Array.from(__mem.subarray(a,a+{count}))}};"""
    return cdp.evaluate('(()=>{' + js + '})()')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', type=int, default=9222)
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('probe')
    c = sub.add_parser('call')
    c.add_argument('--this', dest='this_ptr', type=int, default=DEFAULTS['this'])
    c.add_argument('--a1', type=int, default=DEFAULTS['a1'])
    c.add_argument('--a2', type=int, default=DEFAULTS['a2'])
    c.add_argument('--method', type=int, default=DEFAULTS['method'])
    c.add_argument('--klass', type=int, help='exige que a 1ª palavra do objeto seja esta (segurança)')
    c.add_argument('--listen', type=float, default=4.0)
    a = p.parse_args()

    cdp = CDP(a.port)
    try:
        cdp.attach(network=(a.cmd == 'call'))
        if a.cmd == 'probe':
            info = words(cdp, DEFAULTS['this'])
            print('Objeto', DEFAULTS['this'], '->', info)
            if info and info.get('ok'):
                print(f'klass (1ª palavra) = {info["words"][0]}')
            return 0
        info = words(cdp, a.this_ptr, 2)
        if not info or not info.get('ok'):
            raise SystemExit(f'Não consegui ler a memória do objeto: {info}')
        klass = info['words'][0]
        print(f'Objeto {a.this_ptr}: klass={klass}')
        if a.klass is not None and klass != a.klass:
            raise SystemExit(f'O objeto mudou (klass {klass} != {a.klass}). Não vou chamar.')
        if klass == 0:
            raise SystemExit('Objeto vazio (klass 0). Não vou chamar.')
        js = JS_TABLE + f"""
if(!t)return {{ok:false,error:'tabela indisponível'}};
const f=t.get({DEFAULTS['slot']});
if(typeof f!=='function'||String(f.name)!=='{DEFAULTS['name']}'||f.length!==4)
  return {{ok:false,error:'identidade diferente',name:f&&String(f.name),length:f&&f.length}};
try{{const v=f({a.this_ptr},{a.a1},{a.a2},{a.method});return {{ok:true,returnValue:String(v)}}}}
catch(e){{return {{ok:false,error:String(e)}}}}"""
        # Capture raw 20401/20403 payloads too (CDP class only stores ids), via a second listener.
        raw_frames = []
        orig = cdp.poll

        def poll():
            try:
                m = json.loads(cdp.ws.recv())
            except cdp._ws_mod.WebSocketTimeoutException:
                return
            if m.get('id') is not None:
                cdp.replies[m['id']] = m
                return
            if m.get('method') == 'Network.webSocketFrameReceived':
                resp = m.get('params', {}).get('response', {})
                if resp.get('opcode') == 2:
                    try:
                        raw = base64.b64decode(resp.get('payloadData', ''), validate=True)
                    except ValueError:
                        return
                    if len(raw) >= 8:
                        cdp.frames.append((0, int.from_bytes(raw[4:8], 'big')))
                        raw_frames.append(raw)
        cdp.poll = poll
        start = len(cdp.frames)
        print('Chamando a função da Classificação de Poder UMA vez...')
        result = cdp.evaluate('(()=>{' + js + '})()')
        print('Retorno:', result)
        cdp.collect(a.listen)
        cdp.poll = orig
        ids = [mid for _, mid in cdp.frames[start:]]
        saved = sum(power_ranking.record_frame(r) for r in raw_frames
                    if int.from_bytes(r[4:8], 'big') in power_ranking.RANK_MESSAGE_IDS)
        print(f'Frames recebidos: {len(ids)} | 20403: {ids.count(20403)} | 20401: {ids.count(20401)}')
        if ids.count(20403):
            print(f'✅ Classificação recebida: {saved} jogadores salvos no cache.')
            print(f'   Guarde para segurança: --klass {klass}')
        else:
            print('❌ Nenhuma resposta 20403. Me mande esta saída.')
    finally:
        cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
