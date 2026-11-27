#!/usr/bin/env python3
"""Descobre quais funções WASM do jogo enviam um pedido (ex.: abrir a Classificação de Poder).

Só observa: envolve WebSocket.prototype.send para guardar a pilha de chamadas (stack trace)
de cada envio, sem alterar os dados, e remove o gancho no final.

Uso (Chrome de depuração aberto, logado, com o mapa na tela):
    .venv-mac/bin/python wasm_tools/trace_requests.py --seconds 40
Durante o tempo, abra Classificação -> Classificação Local -> Classificação de Poder,
feche, e abra de novo (2 vezes ajuda a confirmar).

Gera wasm_tools/captures/trace_<hora>.json e mostra, para cada resposta 20401/20403,
o pedido enviado logo antes e as funções WASM da pilha (com slot na tabela, se houver).
"""
import argparse
import base64
import datetime as dt
import json
from pathlib import Path
import re
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from wasm_diagnose import CDP, BUILDS  # noqa: E402
import wasm_analyze as wa  # noqa: E402

TARGET_IDS = (20401, 20403)

HOOK = r"""(()=>{
if(window.__gotwicSendOrig)return {ok:true,already:true};
Error.stackTraceLimit=400;
const orig=WebSocket.prototype.send; window.__gotwicSendOrig=orig;
const log=[]; window.__gotwicSendLog=log;
WebSocket.prototype.send=function(d){
  try{
    let u8=null;
    if(d instanceof ArrayBuffer)u8=new Uint8Array(d);
    else if(ArrayBuffer.isView(d))u8=new Uint8Array(d.buffer,d.byteOffset,d.byteLength);
    const head=u8?Array.from(u8.subarray(0,12)).map(b=>b.toString(16).padStart(2,'0')).join(''):null;
    log.push({seq:log.length,n:u8?u8.byteLength:(d&&d.length),head,stack:String(new Error().stack)});
    if(log.length>400)log.shift();
  }catch(e){}
  return orig.apply(this,arguments);
};
return {ok:true};})()"""

UNHOOK = r"""(()=>{
if(window.__gotwicSendOrig){WebSocket.prototype.send=window.__gotwicSendOrig;delete window.__gotwicSendOrig;}
const log=window.__gotwicSendLog||[];delete window.__gotwicSendLog;Error.stackTraceLimit=10;
return log;})()"""

WASM_FRAME = re.compile(r'wasm-function\[(\d+)\]')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', type=int, default=9222)
    p.add_argument('--seconds', type=float, default=40)
    a = p.parse_args()

    cdp = CDP(a.port)
    events = []          # ordered: ('sent', len, head) / ('recv', message id)
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
        elif method in ('Network.webSocketFrameSent', 'Network.webSocketFrameReceived'):
            resp = params.get('response', {})
            if resp.get('opcode') != 2:
                return
            try:
                raw = base64.b64decode(resp.get('payloadData', ''), validate=True)
            except ValueError:
                return
            if method.endswith('Sent'):
                events.append(('sent', len(raw), raw[:12].hex()))
            elif len(raw) >= 8:
                events.append(('recv', int.from_bytes(raw[4:8], 'big')))
    cdp.poll = poll
    log = []
    try:
        cdp.attach()
        hooked = cdp.evaluate(HOOK)
        if not (isinstance(hooked, dict) and hooked.get('ok')):
            raise SystemExit(f'Não consegui instalar o gancho: {hooked}')
        cdp.wait(cdp.send('Network.enable', {'maxTotalBufferSize': 16000000}))
        print(f'Gravando {a.seconds:g}s — abra Classificação > Classificação Local > Classificação de Poder '
              '(feche e abra de novo uma vez)...', flush=True)
        cdp.collect(a.seconds)
    finally:
        try:
            log = cdp.evaluate(UNHOOK) or []
        except Exception as exc:
            print('Aviso: falha ao remover o gancho; recarregue a página do jogo se algo estranho ocorrer:', exc)
        cdp.poll = orig_poll
        cdp.close()

    by_head = {}
    for entry in log:
        if entry.get('head'):
            by_head.setdefault((entry['n'], entry['head']), entry)

    findings = []
    for i, ev in enumerate(events):
        if ev[0] != 'recv' or ev[1] not in TARGET_IDS:
            continue
        sent = next((events[j] for j in range(i - 1, -1, -1) if events[j][0] == 'sent'), None)
        if not sent:
            continue
        entry = by_head.get((sent[1], sent[2]))
        stack = entry['stack'] if entry else ''
        findings.append({'response_id': ev[1], 'request_len': sent[1], 'request_head': sent[2],
                         'wasm_functions': [int(x) for x in WASM_FRAME.findall(stack)],
                         'stack': stack})

    module = None
    builds = sorted(BUILDS.glob('*.wasm'), key=lambda f: f.stat().st_mtime, reverse=True) if BUILDS.exists() else []
    if builds:
        module = wa.load(builds[0])

    out_dir = HERE / 'captures'
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f'trace_{dt.datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    out.write_text(json.dumps({'events': events, 'findings': findings,
                               'send_log_size': len(log), 'wasm_build': builds[0].name if builds else None},
                              indent=1), encoding='utf-8')

    print(f'\nEnvios registrados: {len(log)} | respostas 20401/20403: {len(findings)}')
    if not findings:
        print('Nenhuma resposta de classificação capturada. Rode de novo e abra a Classificação de Poder.')
    for f in findings:
        print(f'\nResposta {f["response_id"]} <- pedido de {f["request_len"]} bytes')
        if not f['wasm_functions']:
            print('  (pilha sem funções WASM — o envio pode vir de uma fila)')
        for idx in f['wasm_functions'][:40]:
            if module and 0 <= idx < module.n_functions:
                d = wa.describe(module, idx)
                print(f'  função {idx:>7}  {d["sig"]:<32} {d["size"] or 0:>6} bytes  slots {d["slots"][:3]}')
            else:
                print(f'  função {idx}')
    print(f'\nArquivo: {out.relative_to(ROOT)} — me avise que eu analiso.')


if __name__ == '__main__':
    main()
