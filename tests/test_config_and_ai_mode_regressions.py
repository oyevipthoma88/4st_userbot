import ast
import os
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCHED = False

def source(name):
    s = (ROOT / name).read_text()
    if PATCHED and name == 'main.py':
        for flag in ('MASTER_SYNC', 'SAFE_MODE'):
            tree = ast.parse(s)
            node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == 'data' and isinstance(t.slice, ast.Constant) and t.slice.value == flag for t in n.targets) and 'bool(' in ast.get_source_segment(s,n))
            s = s.replace(ast.get_source_segment(s,node), f'data["{flag}"] = str(data.get("{flag}", False)).strip().lower() in {{"1", "true", "yes", "on"}}')
        s = s.replace('ai_modes[chat_id]', 'ai_modes[(my_id, chat_id)]').replace('ai_modes.pop(chat_id, None)', 'ai_modes.pop((my_id, chat_id), None)').replace('if cid in ai_modes and', 'if (my_id, cid) in ai_modes and').replace('mode = ai_modes[cid]', 'mode = ai_modes[(my_id, cid)]')
    if PATCHED and name == 'music_sources.py':
        replacements = {
            '_DIRECT_STREAM_RETRIES = max(1, int(os.environ.get("MUSIC_DIRECT_RETRIES", "3")))': 'try:\n    _DIRECT_STREAM_RETRIES = max(1, int(os.environ.get("MUSIC_DIRECT_RETRIES", "3")))\nexcept (TypeError, ValueError):\n    _DIRECT_STREAM_RETRIES = 3',
            'FFPROBE_GATE_TIMEOUT = float(os.environ.get("MUSIC_PROBE_TIMEOUT", "2.5") or 2.5)': 'try:\n    FFPROBE_GATE_TIMEOUT = float(os.environ.get("MUSIC_PROBE_TIMEOUT", "2.5") or 2.5)\nexcept (TypeError, ValueError):\n    FFPROBE_GATE_TIMEOUT = 2.5',
            '_BGUTIL_HTTP_PORT: int = int(os.environ.get("BGUTIL_PORT", "4416"))': 'try:\n    _BGUTIL_HTTP_PORT: int = int(os.environ.get("BGUTIL_PORT", "4416"))\nexcept (TypeError, ValueError):\n    _BGUTIL_HTTP_PORT = 4416',
        }
        for a,b in replacements.items():
            assert a in s
            s=s.replace(a,b)
    return s

@pytest.mark.parametrize('flag', ['MASTER_SYNC', 'SAFE_MODE'])
@pytest.mark.parametrize('value', ['FALSE', ' off ', 'no', 'garbage'])
def test_false_flags(flag, value):
    s=source('main.py')
    node=next(n for n in ast.walk(ast.parse(s)) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id=='data' and isinstance(t.slice, ast.Constant) and t.slice.value==flag for t in n.targets))
    ns={'data': {flag:value}}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual config assignment>','exec'),ns)
    assert ns['data'][flag] is False

def test_ai_mode_is_account_scoped():
    s=source('main.py')
    tree=ast.parse(s)
    write=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Subscript) and isinstance(t.value,ast.Name) and t.value.id=='ai_modes' for t in n.targets))
    reader=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and ast.get_source_segment(s,n.test).startswith('(my_id, cid) in ai_modes'))
    ns={'ai_modes':{},'my_id':1,'chat_id':99,'mode':'normal'}
    exec(compile(ast.Module(body=[write],type_ignores=[]),'actual writer','exec'),ns)
    ns.update(my_id=2,cid=99,event=type('Event',(),{'text':'hello'})())
    assert not eval(compile(ast.Expression(reader.test),'actual reader','eval'),ns)

@pytest.mark.parametrize('env,target,default', [('MUSIC_DIRECT_RETRIES','_DIRECT_STREAM_RETRIES',3),('MUSIC_PROBE_TIMEOUT','FFPROBE_GATE_TIMEOUT',2.5),('BGUTIL_PORT','_BGUTIL_HTTP_PORT',4416)])
def test_bad_optional_env_does_not_break_import(monkeypatch,env,target,default):
    monkeypatch.setenv(env,'invalid')
    tree=ast.parse(source('music_sources.py'))
    def assigns(n):
        return any(isinstance(x,ast.Name) and isinstance(x.ctx,ast.Store) and x.id==target for x in ast.walk(n))
    node=next(n for n in tree.body if assigns(n))
    ns={'os':os}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'actual startup statement','exec'),ns)
    assert ns[target]==default
