# -*- coding: utf-8 -*-
"""MCP（Model Context Protocol）客户端

让 Agent 能挂外部 MCP 服务器：本地 stdio 子进程，JSON-RPC 2.0 逐行传输。

配置在 ``data/mcp.json``：
    {"servers": {"名字": {"command": "npx", "args": ["-y", "@xxx/mcp"],
                          "env": {"KEY": "值"}, "enabled": true}}}

设计取舍
--------
* **懒连接**：只有被显式 connect（或启动预热）才会 spawn 子进程，
  不会因为配了一个跑不起来的服务器就让每次对话都卡住。
* **失败缓存**：连不上记下错误，60 秒内不重试，界面能看到原因。
* **工具命名** ``mcp__<服务器>__<工具>``：前缀区分来源，调用时按前缀分流。
* **只读模式不下发**：MCP 工具可能改外部系统，只读档一律不挂给模型。
* **启动即视为授权**：用户在配置里启用某个服务器，就是允许 AI 在对话里用它。
  这些工具不受本系统 wid 撤销保护，所以界面上要写清楚。
"""
import atexit
import json
import os
import queue
import shutil
import subprocess
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
CFG_PATH = os.path.join(DATA_DIR, 'mcp.json')

INIT_TIMEOUT = 12
CALL_TIMEOUT = 40
PROTOCOL_VERSION = '2024-11-05'
RETRY_AFTER = 60            # 连接失败后多久再允许重试

_LOCK = threading.Lock()
_CLIENTS = {}


# ---------------------------------------------------------------- 配置
def _defaults():
    return {'servers': {}}


def load_cfg():
    try:
        with open(CFG_PATH, encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return _defaults()
    if not isinstance(raw, dict):
        return _defaults()
    servers = raw.get('servers')
    if not isinstance(servers, dict):
        servers = {}
    out = {}
    for name, c in servers.items():
        if not isinstance(c, dict):
            continue
        cmd = str(c.get('command') or '').strip()
        if not cmd:
            continue
        out[str(name)] = {
            'command': cmd,
            'args': [str(x) for x in (c.get('args') or [])],
            'env': {str(k): str(v) for k, v in (c.get('env') or {}).items()},
            'enabled': bool(c.get('enabled', True)),
        }
    return {'servers': out}


def save_cfg(cfg):
    servers = {}
    for name, c in ((cfg or {}).get('servers') or {}).items():
        cmd = str((c or {}).get('command') or '').strip()
        if not cmd:
            continue
        servers[str(name)] = {
            'command': cmd,
            'args': [str(x) for x in (c.get('args') or [])],
            'env': {str(k): str(v) for k, v in (c.get('env') or {}).items()},
            'enabled': bool(c.get('enabled', True)),
        }
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CFG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'servers': servers}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG_PATH)
    close_all()
    return {'servers': servers}


def upsert_server(name, spec):
    name = str(name or '').strip()
    if not name:
        raise ValueError('服务器名字不能为空')
    cfg = load_cfg()
    cfg['servers'][name] = {
        'command': str((spec or {}).get('command') or '').strip(),
        'args': [str(x) for x in ((spec or {}).get('args') or [])],
        'env': {str(k): str(v) for k, v in ((spec or {}).get('env') or {}).items()},
        'enabled': bool((spec or {}).get('enabled', True)),
    }
    if not cfg['servers'][name]['command']:
        raise ValueError('command 不能为空，例如 npx / python / node')
    return save_cfg(cfg)


def remove_server(name):
    cfg = load_cfg()
    if name not in cfg['servers']:
        return False
    cfg['servers'].pop(name, None)
    save_cfg(cfg)
    return True


# ---------------------------------------------------------------- 子进程客户端
class Client:
    def __init__(self, name, spec):
        self.name = name
        self.spec = spec
        self.proc = None
        self.tools = []
        self.error = ''
        self.connected_at = 0
        self.failed_at = 0
        self._id = 0
        self._boxes = {}
        self._io = threading.Lock()
        self._stderr = []

    # ---- 进程
    def _popen(self):
        cmd = self.spec['command']
        args = list(self.spec.get('args') or [])
        full = shutil.which(cmd) or cmd
        use_shell = full.lower().endswith(('.cmd', '.bat'))
        env = dict(os.environ)
        env.update(self.spec.get('env') or {})
        env.setdefault('PYTHONIOENCODING', 'utf-8')
        kw = {}
        if os.name == 'nt':
            kw['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        return subprocess.Popen(
            (' '.join([full] + [f'"{a}"' if ' ' in a else a for a in args])
             if use_shell else [full] + args),
            shell=use_shell,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, text=True, encoding='utf-8',
            errors='replace', bufsize=1, **kw)

    def _send(self, obj):
        p = self.proc
        if not p or p.poll() is not None:
            raise RuntimeError(f'{self.name} 没在运行')
        p.stdin.write(json.dumps(obj, ensure_ascii=False) + '\n')
        p.stdin.flush()

    def _reader(self):
        p = self.proc
        try:
            for line in p.stdout:
                line = (line or '').strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                rid = msg.get('id')
                if rid is None:
                    continue              # 服务器主动通知，忽略
                box = self._boxes.get(rid)
                if box is not None:
                    box.put(msg)
        except Exception:
            pass

    def _err_reader(self):
        try:
            for line in self.proc.stderr:
                s = (line or '').strip()
                if s:
                    self._stderr.append(s)
                    del self._stderr[:-40]
        except Exception:
            pass

    def request(self, method, params=None, timeout=CALL_TIMEOUT):
        with self._io:
            if not self.proc or self.proc.poll() is not None:
                raise RuntimeError(f'{self.name} 进程已退出')
            self._id += 1
            rid = self._id
            box = queue.Queue()
            self._boxes[rid] = box
            try:
                self._send({'jsonrpc': '2.0', 'id': rid,
                            'method': method, 'params': params or {}})
                try:
                    msg = box.get(timeout=timeout)
                except queue.Empty:
                    raise RuntimeError(f'{self.name} 在 {timeout}s 内没有响应')
            finally:
                self._boxes.pop(rid, None)
        if isinstance(msg, dict) and msg.get('error'):
            err = msg['error']
            raise RuntimeError(err.get('message') if isinstance(err, dict)
                               else str(err))
        return (msg or {}).get('result') or {}

    # ---- 连接
    def connect(self, force=False):
        now = time.time()
        if self.proc and self.proc.poll() is None and self.tools:
            return self.tools
        if not force and self.failed_at and now - self.failed_at < RETRY_AFTER:
            raise RuntimeError(self.error or '上次连接失败，稍后再试')

        self.close()
        self.error = ''
        try:
            self.proc = self._popen()
            threading.Thread(target=self._reader, daemon=True).start()
            threading.Thread(target=self._err_reader, daemon=True).start()
            self.request('initialize', {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {},
                'clientInfo': {'name': 'journal-bi', 'version': '1.0'},
            }, timeout=INIT_TIMEOUT)
            try:
                self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            except Exception:
                pass
            self.tools = self._list_tools()
            self.connected_at = time.time()
            self.failed_at = 0
            return self.tools
        except Exception as exc:
            self.error = f'{type(exc).__name__}: {exc}'
            if not self.error.strip() or self.error.endswith(': '):
                self.error = f'启动失败：{self.spec.get("command")}'
            tail = ' / '.join(self._stderr[-3:])
            if tail:
                self.error += f'（{tail[:200]}）'
            self.failed_at = time.time()
            self.close()
            raise RuntimeError(self.error)

    def _list_tools(self):
        out, cursor = [], None
        for _ in range(10):
            params = {'cursor': cursor} if cursor else {}
            res = self.request('tools/list', params, timeout=INIT_TIMEOUT)
            for t in (res.get('tools') or []):
                if isinstance(t, dict) and t.get('name'):
                    out.append(t)
            cursor = res.get('nextCursor')
            if not cursor:
                break
        return out

    def call(self, tool, args):
        res = self.request('tools/call', {'name': tool, 'arguments': args or {}})
        parts = []
        for c in (res.get('content') or []):
            if not isinstance(c, dict):
                continue
            if c.get('type') == 'text':
                parts.append(str(c.get('text') or ''))
            elif c.get('type') == 'resource':
                r = c.get('resource') or {}
                parts.append(str(r.get('text') or r.get('uri') or ''))
            elif c.get('type') == 'image':
                parts.append('[图片结果，未展示]')
        text = '\n'.join(p for p in parts if p).strip()
        if not text:
            text = json.dumps(res, ensure_ascii=False)[:2000]
        return text, bool(res.get('isError'))

    def close(self):
        p, self.proc = self.proc, None
        self.tools = []
        if not p:
            return
        try:
            p.stdin and p.stdin.close()
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


# ---------------------------------------------------------------- 对外
def _enabled():
    return {k: v for k, v in load_cfg()['servers'].items() if v.get('enabled', True)}


def _client(name):
    spec = _enabled().get(name)
    if not spec:
        return None
    with _LOCK:
        c = _CLIENTS.get(name)
        if not c:
            c = Client(name, spec)
            _CLIENTS[name] = c
        return c


def connect(name, force=False):
    c = _client(name)
    if not c:
        raise RuntimeError(f'没有启用中的 MCP 服务器 {name}')
    return c.connect(force=force)


def connect_all(force=False):
    """启动预热：后台逐个连，失败的记下原因，不抛。"""
    out = {}
    for name in list(_enabled()):
        try:
            out[name] = len(connect(name, force=force))
        except Exception as exc:
            out[name] = f'失败：{exc}'
    return out


def warm():
    """在后台线程里预热，别拖慢应用启动。"""
    t = threading.Thread(target=connect_all, daemon=True)
    t.start()
    return t


def tools():
    """已连接服务器的工具 → OpenAI function 定义。只读安全：调用方决定要不要挂。"""
    out = []
    for name in list(_enabled()):
        c = _CLIENTS.get(name)
        if not c or not c.tools:
            continue
        for t in c.tools:
            fname = f'mcp__{name}__{_safe(t.get("name"))}'
            schema = t.get('inputSchema') or {'type': 'object', 'properties': {}}
            if not isinstance(schema, dict):
                schema = {'type': 'object', 'properties': {}}
            desc = (t.get('description') or '').strip() or f'{name} 提供的工具'
            out.append({'type': 'function', 'function': {
                'name': fname[:64],
                'description': f'[MCP:{name}] {desc}'[:1000],
                'parameters': {'type': 'object',
                               'properties': schema.get('properties') or {},
                               'required': schema.get('required') or []},
            }, '_mcp': {'server': name, 'tool': t.get('name')}})
    return out


def _safe(s):
    s = ''.join(ch if (ch.isalnum() or ch in '_-') else '_'
                for ch in str(s or 'tool'))
    return s or 'tool'


def call(fname, args):
    """调用 ``mcp__<server>__<tool>``。返回 (文本, 是否出错)。"""
    parts = str(fname or '').split('__')
    if len(parts) < 3 or parts[0] != 'mcp':
        return '工具名不对', True
    server = parts[1]
    raw = '__'.join(parts[2:])
    c = _client(server)
    if not c:
        return f'没有启用中的 MCP 服务器 {server}', True
    # 名字被 _safe 处理过，回查原始名
    real = raw
    for t in (c.tools or []):
        if _safe(t.get('name')) == raw:
            real = t.get('name')
            break
    if not c.tools:
        try:
            c.connect()
        except Exception as exc:
            return f'连接 {server} 失败：{exc}', True
    try:
        text, is_err = c.call(real, args or {})
    except Exception as exc:
        return f'{server}/{real} 调用失败：{type(exc).__name__}: {exc}', True
    return text, is_err


def status():
    cfg = load_cfg()
    out = []
    for name, spec in cfg['servers'].items():
        c = _CLIENTS.get(name)
        item = {
            'name': name,
            'command': spec['command'],
            'args': spec.get('args') or [],
            'env_keys': sorted((spec.get('env') or {}).keys()),
            'enabled': spec.get('enabled', True),
            'connected': bool(c and c.tools),
            'tools': [{'name': t.get('name'),
                       'desc': (t.get('description') or '')[:160]}
                      for t in (c.tools if c else [])],
            'error': (c.error if c else '') or '',
        }
        out.append(item)
    return {'servers': out, 'total_tools': sum(len(x['tools']) for x in out)}


def close_all():
    with _LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for c in clients:
        try:
            c.close()
        except Exception:
            pass


atexit.register(close_all)
