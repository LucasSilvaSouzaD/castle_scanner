"""Ask the game for the kingdom Power ranking without opening its window.

Calls the game's own request method (WASM function 62398, table slot 76143) with the
manager object and MethodInfo found through fixed memory paths. Verified on build
gotwic_2cafd796d485 (2026-11-25) across a page reload:
    object = mem[mem[mem[6664780] + 92] + 4]    (mem[6664780] is the object's class pointer)
    method = mem[6964308]
The server answers with 20403 (top 100 by power); the scanner's frame handler stores it in
ranking/power_ranking.json. Every value is checked before the call; on any mismatch the
request is skipped and the scan continues normally.

Values live in wasm_reference/rank_request.json so they can be updated after a game update.
"""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / 'wasm_reference' / 'rank_request.json'
DEFAULT = {
    'build': 'gotwic_2cafd796d485',
    'slot': 76143, 'name': '62398', 'length': 4,
    'object_root': 6664780, 'object_offsets': [92, 4],
    'method_root': 6964308,
    'args': [1, 1],
}


def config():
    try:
        data = json.loads(CONFIG.read_text(encoding='utf-8'))
        return {**DEFAULT, **data}
    except (OSError, ValueError):
        return dict(DEFAULT)


def request_js(cfg=None):
    c = cfg or config()
    return """(()=>{const M=window.gameInstance?.Module;
const t=M?.asm?.__indirect_function_table;
const H=M?.HEAPU32||(M?.wasmMemory?new Uint32Array(M.wasmMemory.buffer):null)||(M?.asm?.memory?new Uint32Array(M.asm.memory.buffer):null);
if(!t||!H)return {ok:false,error:'table/memory unavailable'};
const rd=a=>(a>0&&(a>>>2)<H.length)?H[a>>>2]:0;
const klass=rd(%(root)d);let obj=klass;
for(const o of %(offs)s){if(!obj)break;obj=rd(obj+o);}
if(!klass||!obj)return {ok:false,error:'object path empty'};
if(rd(obj)!==klass)return {ok:false,error:'object class mismatch',klass,obj};
const method=rd(%(mroot)d);if(!method)return {ok:false,error:'method path empty'};
const f=t.get(%(slot)d);
if(typeof f!=='function'||String(f.name)!=='%(name)s'||f.length!==%(length)d)
  return {ok:false,error:'function identity mismatch',name:f&&String(f.name)};
try{f(obj,%(a1)d,%(a2)d,method);return {ok:true,object:obj,method}}
catch(e){return {ok:false,error:String(e)}}})()""" % {
        'root': c['object_root'], 'offs': json.dumps(c['object_offsets']), 'mroot': c['method_root'],
        'slot': c['slot'], 'name': c['name'], 'length': c['length'], 'a1': c['args'][0], 'a2': c['args'][1]}
