#!/usr/bin/env python3
"""Diagnóstico WASM ao vivo do GOTWiC Scanner (via Chrome DevTools Protocol).

Pré-requisito: Chrome aberto com --remote-debugging-port (RUN_MAC.command já faz isso),
logado no jogo e com o MAPA aberto.

Rode com o Python do ambiente do scanner, a partir da pasta do projeto:
    .venv-mac/bin/python wasm_tools/wasm_diagnose.py <comando>

Comandos
  check            Confere se os slots/funções que o scanner usa ainda batem com o jogo.
                   Só leitura, não chama nenhuma função do jogo.
  find             Procura na tabela WASM ao vivo o par navegação/reino pela regra estrutural
                   (nav em S com 5 params, reino em S-3 com 6 params, índice +20..+40). Só leitura.
  test             Faz UMA chamada de navegação controlada num slot candidato e conta os
                   frames 9001 que chegam (o mapa deve se mover para X=125 Y=325 por padrão).
  test-kingdom     Faz UMA chamada do seletor de reino num slot candidato.
  apply            Grava os novos slots/funções no gotwic_scanner_v2_2.py (com backup).
  dump             Baixa o binário .wasm que está rodando para wasm_builds/.
  save-reference   (com o scanner FUNCIONANDO) dump + impressão digital das funções
                   verificadas em wasm_reference/. Faça isso a cada versão que funciona!
  recover          Faz tudo que é seguro automaticamente: check -> find -> dump -> análise
                   offline -> comparação com a referência salva, e mostra os comandos
                   exatos de test/apply. Não chama funções do jogo.
"""
import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import wasm_analyze as wa  # noqa: E402

SCANNER = ROOT / 'gotwic_scanner_v2_2.py'
BUILDS = ROOT / 'wasm_builds'
REFS = ROOT / 'wasm_reference'
GAME_ORIGIN = 'logins-gotpc.gtarcade.com'
SAFE_TEST_XY = (125, 325)          # água; o mesmo ponto que o scanner usa para fechar popups
DELTA_MIN, DELTA_MAX = wa.PAIR_INDEX_DELTA
PAIR_OFFSET = wa.PAIR_SLOT_OFFSET

# Same lookup path the scanner uses, plus fallbacks used only for diagnosis.
JS_TABLE = """const __gi=window.gameInstance;
const __M=__gi?.Module||window.unityInstance?.Module||window.Module;
const __scannerTable=window.gameInstance?.Module?.asm?.__indirect_function_table;
const t=__scannerTable||__M?.wasmTable||__M?.asm?.table||null;"""


# ----------------------------------------------------------------- scanner constants
def _block(text, name):
    m = re.search(name + r'\s*=\s*"""(.*?)"""', text, re.S)
    if not m:
        raise SystemExit(f'Bloco {name} não encontrado em {SCANNER.name}')
    return m


def read_expected(text=None):
    text = text if text is not None else SCANNER.read_text(encoding='utf-8')
    out = {}
    for key, block in (('nav', 'NAV'), ('kingdom', 'KINGDOM_SELECT')):
        body = _block(text, block).group(1)
        out[key] = {'slot': int(re.search(r't\.get\((\d+)\)', body).group(1)),
                    'name': re.search(r"f\.name!==?'(\d+)'", body).group(1),
                    'length': int(re.search(r'f\.length!==?(\d+)', body).group(1))}
    pre = _block(text, 'PREFLIGHT').group(1)
    out['preflight_slot'] = int(re.search(r't\.get\((\d+)\)', pre).group(1))
    return out


# ----------------------------------------------------------------- CDP client
class CDP:
    def __init__(self, port):
        import websocket
        self._ws_mod = websocket
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=5) as r:
                pages = json.load(r)
        except OSError as exc:
            raise SystemExit(f'Chrome não responde em 127.0.0.1:{port} ({exc}).\n'
                             'Abra pelo RUN_MAC.command (ou com --remote-debugging-port) e carregue o jogo.')
        page = next((q for q in pages if q.get('type') == 'page' and 'gtarcade' in q.get('url', '').lower()
                     and q.get('webSocketDebuggerUrl')), None)
        if not page:
            raise SystemExit('Aba do GTArcade não encontrada no Chrome de depuração. Abra o jogo nessa janela.')
        self.ws = websocket.create_connection(page['webSocketDebuggerUrl'], timeout=10, suppress_origin=True)
        self.ws.settimeout(.05)
        self.seq = 0
        self.replies = {}
        self.contexts = []
        self.frames = []     # (monotonic time, message id) of incoming binary frames

    def send(self, method, params=None):
        self.seq += 1
        self.ws.send(json.dumps({'id': self.seq, 'method': method, 'params': params or {}}))
        return self.seq

    def poll(self):
        try:
            m = json.loads(self.ws.recv())
        except self._ws_mod.WebSocketTimeoutException:
            return
        if m.get('id') is not None:
            self.replies[m['id']] = m
            return
        method = m.get('method')
        params = m.get('params', {})
        if method == 'Runtime.executionContextCreated':
            self.contexts.append(params.get('context', {}))
        elif method == 'Network.webSocketFrameReceived':
            resp = params.get('response', {})
            if resp.get('opcode') == 2:
                try:
                    raw = base64.b64decode(resp.get('payloadData', ''), validate=True)
                    if len(raw) >= 13:
                        self.frames.append((time.monotonic(), int.from_bytes(raw[4:8], 'big')))
                except ValueError:
                    pass

    def wait(self, rid, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if rid in self.replies:
                return self.replies.pop(rid)
            self.poll()
        raise TimeoutError(f'CDP: sem resposta para a requisição {rid}')

    def collect(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.poll()

    def attach(self, network=False):
        self.wait(self.send('Runtime.enable'))
        if network:
            self.wait(self.send('Network.enable', {'maxTotalBufferSize': 16000000}))
        self.collect(.3)
        valid = [c for c in self.contexts if c.get('auxData', {}).get('isDefault') and GAME_ORIGIN in c.get('origin', '')]
        if not valid:
            origins = sorted({c.get('origin', '?') for c in self.contexts})
            raise SystemExit('Contexto do Unity não encontrado (origem esperada: ' + GAME_ORIGIN + ').\n'
                             'Origens vistas: ' + ', '.join(origins) +
                             '\nSe o jogo mudou de domínio, o scanner inteiro precisa ser ajustado.')
        self.cid = valid[0]['id']
        return self.cid

    def evaluate(self, expr, timeout=15, await_promise=False):
        params = {'expression': expr, 'contextId': self.cid, 'returnByValue': True,
                  'silent': True, 'timeout': int(timeout * 1000)}
        if await_promise:
            params['awaitPromise'] = True
        resp = self.wait(self.send('Runtime.evaluate', params), timeout + 5)
        if 'error' in resp:
            raise RuntimeError(str(resp['error']))
        result = resp.get('result', {})
        if 'exceptionDetails' in result:
            raise RuntimeError(str(result['exceptionDetails'].get('exception', {}).get('description')
                                   or result['exceptionDetails']))
        return result.get('result', {}).get('value')

    def close(self):
        for method in ('Network.disable', 'Runtime.disable'):
            try:
                self.wait(self.send(method), 3)
            except Exception:
                pass
        self.ws.close()


def _strip(url):
    return str(url or '').split('?', 1)[0].split('#', 1)[0]


# ----------------------------------------------------------------- live queries
def live_state(cdp, exp):
    js = JS_TABLE + """
const id=s=>{try{const f=t.get(s);return f?{slot:s,name:String(f.name),length:f.length}:{slot:s,empty:true}}
             catch(e){return {slot:s,error:String(e)}}};
const out={scannerPath:!!__scannerTable,hasGameInstance:!!__gi,hasModule:!!__M,tableLength:t?t.length:null,
  unityVersion:__M?.unityVersion||__gi?.unityVersion||null};
if(t){out.nav=id(NAV_SLOT);out.kingdom=id(K_SLOT);out.navNameAt=[];out.kingdomNameAt=[];
  for(let i=0;i<t.length;i++){let f;try{f=t.get(i)}catch(e){continue}if(!f)continue;
    if(String(f.name)==='NAV_NAME')out.navNameAt.push(i);if(String(f.name)==='K_NAME')out.kingdomNameAt.push(i);}}
out.resources=performance.getEntriesByType('resource').map(e=>e.name)
  .filter(u=>/wasm|unityweb|framework|loader|\\/Build\\//i.test(u)).slice(0,40);
return out;"""
    js = (js.replace('NAV_SLOT', str(exp['nav']['slot'])).replace('K_SLOT', str(exp['kingdom']['slot']))
            .replace('NAV_NAME', exp['nav']['name']).replace('K_NAME', exp['kingdom']['name']))
    return cdp.evaluate('(()=>{' + js + '})()')


def verdict(state, exp):
    if not state or not state.get('tableLength'):
        return False, 'Tabela WASM indisponível (o jogo carregou? o mapa está aberto?)'
    if not state.get('scannerPath'):
        return False, ('A tabela não está em gameInstance.Module.asm.__indirect_function_table '
                       '(o Unity/loader mudou). O JS do scanner precisa ser ajustado além dos números.')
    problems = []
    for key in ('nav', 'kingdom'):
        live, want = state[key], exp[key]
        if live.get('name') != want['name'] or live.get('length') != want['length']:
            problems.append(f'{key}: slot {want["slot"]} esperado função {want["name"]}/{want["length"]} params, '
                            f'encontrado {live.get("name", "vazio")}/{live.get("length", "-")}')
    return (not problems), ('; '.join(problems) if problems else 'OK')


def live_pairs(cdp):
    js = JS_TABLE + """
if(!t)return {error:'tabela indisponível'};
const res=[];let total=0;
for(let s=3;s<t.length;s++){let f,g;try{f=t.get(s)}catch(e){continue}
  if(!f||f.length!==5)continue;try{g=t.get(s+OFFSET)}catch(e){continue}
  if(!g||g.length!==6)continue;const d=Number(g.name)-Number(f.name);
  if(d>=DMIN&&d<=DMAX){total++;if(res.length<300)res.push({navSlot:s,navName:String(f.name),
     kingdomSlot:s+OFFSET,kingdomName:String(g.name),delta:d});}}
return {tableLength:t.length,total,candidates:res};"""
    js = js.replace('OFFSET', str(PAIR_OFFSET)).replace('DMIN', str(DELTA_MIN)).replace('DMAX', str(DELTA_MAX))
    return cdp.evaluate('(()=>{' + js + '})()', timeout=30)


def dump_binary(cdp, extra_url=None):
    prep = JS_TABLE + """
const asU8=b=>b instanceof Uint8Array?b:(b instanceof ArrayBuffer?new Uint8Array(b):(ArrayBuffer.isView(b)?new Uint8Array(b.buffer,b.byteOffset,b.byteLength):null));
let bytes=null,source=null;
if(__M&&__M.wasmBinary){bytes=asU8(__M.wasmBinary);source='Module.wasmBinary';}
const urls=[...new Set([...EXTRA_URLS,__M?.wasmCodeUrl,__M?.codeUrl,__gi?.Module?.wasmCodeUrl,
  ...performance.getEntriesByType('resource').map(e=>e.name).filter(u=>/wasm/i.test(u))].filter(Boolean))];
if(!bytes||!(bytes[0]===0&&bytes[1]===0x61)){
  for(const u of urls){try{const r=await fetch(u,{credentials:'include'});const b=new Uint8Array(await r.arrayBuffer());
    if(b.length>1000){bytes=b;source=u;if(b[0]===0&&b[1]===0x61)break;}}catch(e){}}}
if(!bytes)return {ok:false,error:'binário não encontrado',urls:urls.map(u=>u.split('?')[0])};
window.__gotwicWasmDump=bytes;
return {ok:true,size:bytes.length,source:String(source).split('?')[0]};"""
    prep = prep.replace('EXTRA_URLS', json.dumps([extra_url] if extra_url else []))
    info = cdp.evaluate('(async()=>{' + prep + '})()', timeout=120, await_promise=True)
    if not info or not info.get('ok'):
        raise SystemExit(f'Não consegui obter o binário WASM: {info}')
    size, chunk = info['size'], 2 * 1024 * 1024
    parts = []
    for off in range(0, size, chunk):
        js = ('(()=>{const b=window.__gotwicWasmDump;const s=%d,e=Math.min(b.length,s+%d);let x="";'
              'for(let i=s;i<e;i+=0x8000)x+=String.fromCharCode.apply(null,b.subarray(i,Math.min(e,i+0x8000)));'
              'return btoa(x);})()') % (off, chunk)
        parts.append(base64.b64decode(cdp.evaluate(js, timeout=60)))
        print(f'  baixando {min(off + chunk, size) / 1048576:.1f} / {size / 1048576:.1f} MB', end='\r', flush=True)
    print()
    cdp.evaluate('(()=>{delete window.__gotwicWasmDump;return true})()')
    data = b''.join(parts)
    if len(data) != size:
        raise SystemExit('Transferência incompleta do binário.')
    if data[:4] != b'\0asm':
        if data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
        else:
            try:
                import brotli  # type: ignore
                data = brotli.decompress(data)
            except ImportError:
                raise SystemExit('O binário veio comprimido (provavelmente Brotli). Instale: '
                                 '.venv-mac/bin/python -m pip install brotli  e rode de novo.')
    if data[:4] != b'\0asm':
        raise SystemExit('O arquivo obtido não é um WebAssembly válido.')
    sha = hashlib.sha256(data).hexdigest()
    BUILDS.mkdir(exist_ok=True)
    path = BUILDS / f'gotwic_{sha[:12]}.wasm'
    if not path.exists():
        path.write_bytes(data)
    return path, sha, info['source']


def write_meta(path, sha, source, state, extra=None):
    meta = {'wasm_file': path.name, 'sha256': sha, 'size': path.stat().st_size, 'source': source,
            'saved_utc': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            'table_length': state.get('tableLength'), 'unity_version': state.get('unityVersion'),
            'scanner_expected': read_expected()}
    if extra:
        meta.update(extra)
    path.with_suffix('.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')


def offline_check(path, exp_or_values):
    """Make sure the dumped binary is the running build: table slot -> index must agree."""
    m = wa.load(path)
    mismatches = []
    for key, v in exp_or_values.items():
        idx = m.table.get(v['slot'])
        if idx is None or str(idx) != str(v['name']):
            mismatches.append(f'{key}: slot {v["slot"]} no binário = {idx}, ao vivo = {v["name"]}')
    return m, mismatches


# ----------------------------------------------------------------- commands
def cmd_check(a):
    exp = read_expected()
    cdp = CDP(a.port)
    try:
        cdp.attach()
        state = live_state(cdp, exp)
    finally:
        cdp.close()
    ok, msg = verdict(state, exp)
    print(f'Scanner espera  : navegação slot {exp["nav"]["slot"]} função {exp["nav"]["name"]} | '
          f'reino slot {exp["kingdom"]["slot"]} função {exp["kingdom"]["name"]}')
    if state:
        print(f'Jogo ao vivo    : tabela com {state.get("tableLength")} slots | Unity {state.get("unityVersion") or "?"}')
        print(f'  slot {exp["nav"]["slot"]} -> {state.get("nav")}')
        print(f'  slot {exp["kingdom"]["slot"]} -> {state.get("kingdom")}')
        if state.get('navNameAt') is not None:
            print(f'  função {exp["nav"]["name"]} aparece nos slots {state["navNameAt"] or "nenhum"}; '
                  f'função {exp["kingdom"]["name"]} nos slots {state["kingdomNameAt"] or "nenhum"}')
        for u in state.get('resources') or []:
            print('  recurso:', _strip(u))
    print(('\n✅ ' if ok else '\n❌ ') + msg)
    if not ok:
        print('\nO jogo foi atualizado. Rode:  .venv-mac/bin/python wasm_tools/wasm_diagnose.py recover')
    return 0 if ok else 1


def print_pairs(res, near):
    cands = sorted(res.get('candidates', []), key=lambda c: abs(c['navSlot'] - near))
    print(f'Pares candidatos ao vivo: {res.get("total", 0)} (tabela com {res.get("tableLength")} slots)')
    for c in cands[:15]:
        print(f'  nav slot {c["navSlot"]:>6} função {c["navName"]:>7} | reino slot {c["kingdomSlot"]:>6} '
              f'função {c["kingdomName"]:>7} | delta índice +{c["delta"]} | distância do antigo {c["navSlot"] - near:+d}')
    return cands


def cmd_find(a):
    exp = read_expected()
    cdp = CDP(a.port)
    try:
        cdp.attach()
        res = live_pairs(cdp)
    finally:
        cdp.close()
    if res.get('error'):
        raise SystemExit(res['error'])
    cands = print_pairs(res, exp['nav']['slot'])
    if len(cands) == 1:
        c = cands[0]
        print(f'\nUm único candidato. Próximo passo (1 chamada controlada):\n'
              f'  .venv-mac/bin/python wasm_tools/wasm_diagnose.py test --slot {c["navSlot"]} --name {c["navName"]}')
    elif cands:
        print('\nMais de um candidato: rode "recover" para cruzar com a análise do binário.')
    return 0


def cmd_test(a):
    x, y = a.x, a.y
    if not (0 <= x <= 650 and 0 <= y <= 710):
        raise SystemExit('Coordenadas fora da área selecionável (X 0..650, Y 0..710).')
    map_id = y * 720 + x
    cdp = CDP(a.port)
    try:
        cdp.attach(network=True)
        js = JS_TABLE + f"""
if(!t)return {{ok:false,error:'tabela indisponível'}};
const f=t.get({a.slot});
if(typeof f!=='function')return {{ok:false,error:'slot vazio'}};
if(String(f.name)!=='{a.name}'||f.length!==5)return {{ok:false,error:'identidade diferente',name:String(f.name),length:f.length}};
let k=null;try{{const g=t.get({a.slot}+({PAIR_OFFSET}));if(g)k={{slot:{a.slot}+({PAIR_OFFSET}),name:String(g.name),length:g.length}}}}catch(e){{}}
try{{const v=f({map_id},150,200,250,300);return {{ok:true,returnValue:String(v),kingdomPair:k}}}}
catch(e){{return {{ok:false,error:String(e),kingdomPair:k}}}}"""
        before = len(cdp.frames)
        print(f'Chamando slot {a.slot} (função {a.name}) UMA vez -> X={x} Y={y} (map id {map_id}) ...')
        result = cdp.evaluate('(()=>{' + js + '})()')
        cdp.collect(a.listen)
        new = cdp.frames[before:]
    finally:
        cdp.close()
    n9001 = sum(1 for _, mid in new if mid == 9001)
    print(f'Retorno JS      : {result}')
    print(f'Frames recebidos: {len(new)} no total, {n9001} do tipo 9001 (mapa) em {a.listen:g}s')
    good = isinstance(result, dict) and result.get('ok') and result.get('returnValue') == '1' and n9001 > 0
    print('\n' + ('✅ Parece correto. CONFIRME VISUALMENTE que o mapa foi para X=%d Y=%d.' % (x, y) if good else
                  '❌ Não confirmou (retorno diferente de 1 ou nenhum frame 9001). Não aplique este candidato.'))
    if good:
        k = result.get('kingdomPair') or {}
        if k.get('length') == 6:
            print(f'\nSe o mapa se moveu, aplique (reino = slot {k["slot"]}, função {k["name"]}):\n'
                  f'  .venv-mac/bin/python wasm_tools/wasm_diagnose.py apply --nav-slot {a.slot} --nav-name {a.name} '
                  f'--kingdom-slot {k["slot"]} --kingdom-name {k["name"]}')
        else:
            print(f'\nAtenção: o slot {a.slot + PAIR_OFFSET} não tem uma função de 6 parâmetros; '
                  'o seletor de reino precisa ser localizado separadamente ("find"/"recover").')
    return 0 if good else 1


def cmd_test_kingdom(a):
    cdp = CDP(a.port)
    try:
        cdp.attach(network=True)
        js = JS_TABLE + f"""
if(!t)return {{ok:false,error:'tabela indisponível'}};
const f=t.get({a.slot});
if(typeof f!=='function')return {{ok:false,error:'slot vazio'}};
if(String(f.name)!=='{a.name}'||f.length!==6)return {{ok:false,error:'identidade diferente',name:String(f.name),length:f.length}};
try{{const v=f(BigInt(2625310000)+BigInt({a.kingdom}),0,0,0,0,0);return {{ok:true,returnValue:String(v)}}}}
catch(e){{return {{ok:false,error:String(e)}}}}"""
        before = len(cdp.frames)
        print(f'Chamando seletor de reino slot {a.slot} (função {a.name}) UMA vez -> reino {a.kingdom} ...')
        result = cdp.evaluate('(()=>{' + js + '})()')
        cdp.collect(a.listen)
        new = cdp.frames[before:]
    finally:
        cdp.close()
    print(f'Retorno JS      : {result}')
    print(f'Frames recebidos: {len(new)} em {a.listen:g}s (ids: {sorted(set(m for _, m in new))[:15]})')
    print('Confirme visualmente que o jogo trocou para o reino %d.' % a.kingdom)
    return 0 if isinstance(result, dict) and result.get('ok') else 1


def apply_values(text, exp, nav_slot, nav_name, k_slot, k_name):
    def patch_block(text, block, old_slot, new_slot, old_name, new_name):
        m = _block(text, block)
        body = m.group(1)
        body = body.replace(f't.get({old_slot})', f't.get({new_slot})')
        body = body.replace(f'Table slot {old_slot} ', f'Table slot {new_slot} ')
        body = re.sub(r"f\.name!==?'" + re.escape(old_name) + "'",
                      lambda mm: mm.group(0).replace(old_name, str(new_name)), body)
        return text[:m.start(1)] + body + text[m.end(1):]
    text = patch_block(text, 'PREFLIGHT', exp['nav']['slot'], nav_slot, exp['nav']['name'], nav_name)
    text = patch_block(text, 'NAV', exp['nav']['slot'], nav_slot, exp['nav']['name'], nav_name)
    text = patch_block(text, 'KINGDOM_SELECT', exp['kingdom']['slot'], k_slot, exp['kingdom']['name'], k_name)
    return text


def cmd_apply(a):
    text = SCANNER.read_text(encoding='utf-8')
    exp = read_expected(text)
    new = apply_values(text, exp, a.nav_slot, a.nav_name, a.kingdom_slot, a.kingdom_name)
    check = read_expected(new)
    want = {'nav': (a.nav_slot, str(a.nav_name)), 'kingdom': (a.kingdom_slot, str(a.kingdom_name))}
    for key, (slot, name) in want.items():
        if (check[key]['slot'], check[key]['name']) != (slot, name):
            raise SystemExit(f'Falha ao aplicar {key}; arquivo não alterado.')
    if check['preflight_slot'] != a.nav_slot:
        raise SystemExit('Falha ao aplicar PREFLIGHT; arquivo não alterado.')
    if new == text:
        print('Nada a mudar: o scanner já usa esses valores.')
        return 0
    backup = SCANNER.with_name(SCANNER.name + '.bak-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(SCANNER, backup)
    compile(new, SCANNER.name, 'exec')
    SCANNER.write_text(new, encoding='utf-8')
    print(f'Backup : {backup.name}')
    print(f'Antes  : nav {exp["nav"]["slot"]}/{exp["nav"]["name"]}  reino {exp["kingdom"]["slot"]}/{exp["kingdom"]["name"]}')
    print(f'Depois : nav {a.nav_slot}/{a.nav_name}  reino {a.kingdom_slot}/{a.kingdom_name}')
    print('\nAgora rode "check" e depois um scan normal no app para validar de ponta a ponta.\n'
          'Com tudo funcionando, rode "save-reference" para guardar esta versão.')
    return 0


def cmd_dump(a):
    exp = read_expected()
    cdp = CDP(a.port)
    try:
        cdp.attach()
        state = live_state(cdp, exp)
        path, sha, source = dump_binary(cdp, a.wasm_url)
    finally:
        cdp.close()
    write_meta(path, sha, source, state)
    print(f'Salvo: {path.relative_to(ROOT)}  ({path.stat().st_size / 1048576:.1f} MB, sha256 {sha[:16]}…)')
    print(f'Origem: {source}')
    return 0


def cmd_save_reference(a):
    exp = read_expected()
    cdp = CDP(a.port)
    try:
        cdp.attach()
        state = live_state(cdp, exp)
        ok, msg = verdict(state, exp)
        if not ok:
            raise SystemExit('A versão atual do scanner NÃO bate com o jogo; não vou salvar como referência.\n' + msg)
        path, sha, source = dump_binary(cdp, a.wasm_url)
    finally:
        cdp.close()
    m, mism = offline_check(path, {'nav': exp['nav'], 'kingdom': exp['kingdom']})
    if mism:
        raise SystemExit('O binário baixado não corresponde ao jogo em execução: ' + '; '.join(mism))
    write_meta(path, sha, source, state, {'verified': True})
    REFS.mkdir(exist_ok=True)
    ref_path = REFS / f'reference_{sha[:12]}.json'
    ref = {'format': 'gotwic-wasm-reference-v1', 'wasm_sha256': sha, 'wasm_size': len(m.data),
           'source_file': path.name, 'saved_utc': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'targets': {}}
    for key in ('nav', 'kingdom'):
        f = wa.features(m, int(exp[key]['name']))
        f['slot'] = exp[key]['slot']
        ref['targets'][key] = f
    ref_path.write_text(json.dumps(ref, indent=1), encoding='utf-8')
    print(f'✅ Referência salva: {ref_path.relative_to(ROOT)} (binário {path.relative_to(ROOT)})')
    print(f'   nav {ref["targets"]["nav"]["sig"]} {ref["targets"]["nav"]["size"]} bytes | '
          f'reino {ref["targets"]["kingdom"]["sig"]} {ref["targets"]["kingdom"]["size"]} bytes')
    return 0


def latest_reference():
    refs = sorted(REFS.glob('reference_*.json'), key=lambda p: p.stat().st_mtime, reverse=True) if REFS.exists() else []
    return refs[0] if refs else None


def cmd_recover(a):
    exp = read_expected()
    cdp = CDP(a.port)
    try:
        cdp.attach()
        state = live_state(cdp, exp)
        ok, msg = verdict(state, exp)
        print(f'1) Conferência: {"OK" if ok else "QUEBRADO"} — {msg}')
        if ok:
            print('   Nada a recuperar.' + ('' if latest_reference() else
                  ' Dica: rode "save-reference" agora para facilitar a próxima atualização.'))
            return 0
        if not state.get('scannerPath'):
            return 1
        res = live_pairs(cdp)
        print('\n2) Busca estrutural ao vivo (só leitura):')
        live = print_pairs(res, exp['nav']['slot'])
        print('\n3) Baixando o binário WASM atual ...')
        path, sha, source = dump_binary(cdp, a.wasm_url)
    finally:
        cdp.close()
    write_meta(path, sha, source, state)
    print(f'   {path.relative_to(ROOT)} ({path.stat().st_size / 1048576:.1f} MB)')
    m = wa.load(path)
    offline = wa.find_pairs(m, exp['nav']['slot'], min_size=0)
    print(f'\n4) Análise offline com tipos exatos: {len(offline)} par(es) '
          f'(nav {wa.NAV_SIG}, reino ({wa.KINGDOM_PARAMS}) no slot -3)')
    for p in offline[:10]:
        print(f'   nav slot {p["nav_slot"]} função {p["nav_index"]} ({p["nav_size"]} bytes) | '
              f'reino slot {p["kingdom_slot"]} função {p["kingdom_index"]}')
    live_keys = {(c['navSlot'], c['navName']) for c in live}
    agreed = [p for p in offline if (p['nav_slot'], str(p['nav_index'])) in live_keys]
    ref_path = latest_reference()
    best_ref = None
    if ref_path:
        ref = json.loads(ref_path.read_text(encoding='utf-8'))
        print(f'\n5) Comparando com a referência {ref_path.name} ...')
        top_nav, pool = wa.rank(m, ref['targets']['nav'], 5)
        for c in top_nav:
            print(f'   nav função {c["index"]} slots {c["slots"]} score {c["score"]:.3f} '
                  f'(ops {c["opcodes"]:.2f} bytes {c["raw_bytes"]:.2f})')
        if top_nav:
            best_ref = top_nav[0]
    else:
        print('\n5) Sem referência salva (wasm_reference/). Só a regra estrutural será usada.')
    choice = None
    if best_ref:
        choice = next((p for p in agreed if p['nav_index'] == best_ref['index']), None)
    if choice is None and len(agreed) == 1:
        choice = agreed[0]
    if choice is None and best_ref is None:
        # No reference: the verified navigation bodies were ~575 bytes; trivial stubs are not candidates.
        big = [p for p in agreed if p['nav_size'] >= 200]
        if len(big) == 1:
            choice = big[0]
            print('(sem referência: escolhido o único par com corpo de navegação >= 200 bytes)')
    print()
    if choice:
        print('✅ Candidato recomendado (concorda nas verificações disponíveis):')
        print(f'   navegação slot {choice["nav_slot"]} função {choice["nav_index"]} | '
              f'reino slot {choice["kingdom_slot"]} função {choice["kingdom_index"]}')
        print('\nPróximos passos (um de cada vez, com o mapa aberto):')
        print(f'  .venv-mac/bin/python wasm_tools/wasm_diagnose.py test --slot {choice["nav_slot"]} --name {choice["nav_index"]}')
        print(f'  .venv-mac/bin/python wasm_tools/wasm_diagnose.py apply --nav-slot {choice["nav_slot"]} '
              f'--nav-name {choice["nav_index"]} --kingdom-slot {choice["kingdom_slot"]} --kingdom-name {choice["kingdom_index"]}')
        return 0
    print('⚠️  Nenhum candidato único e confirmado. Não aplique às cegas.')
    print('   Veja os pares acima; teste UM de cada vez com "test" e confirme visualmente.')
    return 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', type=int, default=9222)
    p.add_argument('--wasm-url', help='URL do .wasm (se a detecção automática não achar o binário)')
    sub = p.add_subparsers(dest='cmd')
    sub.add_parser('check').set_defaults(fn=cmd_check)
    sub.add_parser('find').set_defaults(fn=cmd_find)
    s = sub.add_parser('test'); s.add_argument('--slot', type=int, required=True); s.add_argument('--name', required=True)
    s.add_argument('--x', type=int, default=SAFE_TEST_XY[0]); s.add_argument('--y', type=int, default=SAFE_TEST_XY[1])
    s.add_argument('--listen', type=float, default=4.0); s.set_defaults(fn=cmd_test)
    s = sub.add_parser('test-kingdom'); s.add_argument('--slot', type=int, required=True)
    s.add_argument('--name', required=True); s.add_argument('--kingdom', type=int, required=True)
    s.add_argument('--listen', type=float, default=4.0); s.set_defaults(fn=cmd_test_kingdom)
    s = sub.add_parser('apply')
    for opt in ('--nav-slot', '--nav-name', '--kingdom-slot', '--kingdom-name'):
        s.add_argument(opt, type=int, required=True)
    s.set_defaults(fn=cmd_apply)
    sub.add_parser('dump').set_defaults(fn=cmd_dump)
    sub.add_parser('save-reference').set_defaults(fn=cmd_save_reference)
    sub.add_parser('recover').set_defaults(fn=cmd_recover)
    a = p.parse_args()
    if not getattr(a, 'fn', None):
        a.fn = cmd_check
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
