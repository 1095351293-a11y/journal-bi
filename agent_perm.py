# -*- coding: utf-8 -*-
"""Agent 访问模式 —— 决定 AI 能做什么、什么必须人点确认

四种模式（参考主流 agent 的 mode 设计）
----------------------------------------
* ``readonly`` 只读      ：写工具根本不下发，AI 只能查
* ``ask``      逐条确认  ：每次写入都先出草稿，人点「确认写入」才落库（默认）
* ``auto``     自动写入  ：记账/改账/日常维护直接写；删除、导入、删公司或账户仍要确认
* ``full``     完全访问  ：任何操作直接执行，不再询问

闸门原则
--------
1. 模式由 ``data/agent_perm.json`` 的 ``mode`` 字段决定，改完即时生效。
2. **只有 ``full`` 模式接受模型自己传的 ``confirm=true``**。其他模式下高危操作
   一律生成「审核卡」，必须由用户在界面上点确认，模型无法自我授权。
3. 恢复备份、任意 SQL、写文件不在这四档之内，永远不暴露给模型。

旧的 ``{"create": "auto", ...}`` 配置读到时按 auto 模式兼容，写回时统一成 mode。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
PERM_PATH = os.path.join(DATA_DIR, 'agent_perm.json')

MODES = ('readonly', 'ask', 'auto', 'full')
DEFAULT_MODE = 'ask'

MODE_INFO = {
    'readonly': {
        'label': '只读',
        'short': '只看不改',
        'desc': 'AI 只能查询，所有写入工具都不下发',
    },
    'ask': {
        'label': '逐条确认',
        'short': '每次都问',
        'desc': '每次写入先出草稿卡，你点「确认写入」才落库',
    },
    'auto': {
        'label': '自动写入',
        'short': '小改直接写',
        'desc': '记账、改账、日常维护直接写；删除、导入、删公司账户仍要你确认',
    },
    'full': {
        'label': '完全访问',
        'short': '全部放行',
        'desc': '任何操作直接执行，不再询问你（仍可撤销流水类写入）',
    },
}

# 每档模式放行哪些写入类型。空集 = 一律出草稿。
_ALLOW = {
    'readonly': frozenset(),
    'ask': frozenset(),
    'auto': frozenset({'create', 'update', 'merge', 'admin'}),
    'full': frozenset({'create', 'update', 'merge', 'delete', 'import',
                       'admin', 'admin_danger'}),
}

KIND_LABEL = {
    'create': '记一笔',
    'update': '改一笔',
    'merge': '合并流水',
    'delete': '删除流水',
    'import': '批量导入对账单',
    'admin': '公司 / 账户 / 标签 / 定期 / 汇率 / 盘点维护',
    'admin_danger': '删公司 / 删账户 / 删定期 / 删盘点单',
}

_CACHE = {'mtime': None, 'data': None}


def _normalize(raw):
    """把各种形态的配置收敛成 {'mode': ...}。"""
    mode = None
    if isinstance(raw, dict):
        m = str(raw.get('mode') or '').strip().lower()
        if m in MODES:
            mode = m
        elif any(k in raw for k in ('create', 'update', 'delete', 'import',
                                    'admin', 'admin_danger')):
            # 老格式：按当时的松紧程度猜一档，别把用户的配置改得更松或更紧太离谱
            old = {k: str(raw.get(k) or '').lower() for k in
                   ('create', 'update', 'admin', 'delete', 'import', 'admin_danger')}
            if old.get('delete') == 'auto' or old.get('import') == 'auto':
                mode = 'full'
            elif old.get('create') == 'auto' or old.get('update') == 'auto':
                mode = 'auto'
            else:
                mode = 'ask'
    return {'mode': mode or DEFAULT_MODE}


def load():
    """读配置；坏文件就当默认，不让配置问题把系统打死。"""
    try:
        mt = os.path.getmtime(PERM_PATH)
    except OSError:
        _CACHE['data'] = {'mode': DEFAULT_MODE}
        _CACHE['mtime'] = None
        return _CACHE['data']
    if _CACHE['mtime'] == mt and _CACHE['data']:
        return _CACHE['data']
    raw = None
    try:
        with open(PERM_PATH, encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = None
    data = _normalize(raw)
    _CACHE['data'] = data
    _CACHE['mtime'] = mt
    return data


def current_mode():
    return load().get('mode') or DEFAULT_MODE


def set_mode(mode):
    m = str(mode or '').strip().lower()
    if m not in MODES:
        raise ValueError(f'mode 只能是 {" / ".join(MODES)}')
    return save({'mode': m})


def is_auto(kind):
    """这个写入类型在当前模式下能不能直接落库。"""
    return kind in _ALLOW.get(current_mode(), frozenset())


def can_bypass():
    """只有完全访问模式，才由模型自己带 confirm=true 执行高危操作。"""
    return current_mode() == 'full'


# 兼容两种叫法
can_bypass_confirm = can_bypass


def allow_write_tools():
    """只读模式下不给任何写工具。"""
    return current_mode() != 'readonly'


def save(patch):
    """合并式写配置。只认 mode，其余键忽略。"""
    cur = load()
    patch = patch or {}
    m = str(patch.get('mode') or '').strip().lower()
    if m in MODES:
        cur = {'mode': m}
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = PERM_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PERM_PATH)
        _CACHE['mtime'] = None
        _CACHE['data'] = None
    except OSError:
        pass
    return cur


def state():
    """给前端/接口用的完整状态。"""
    m = current_mode()
    info = MODE_INFO.get(m) or MODE_INFO[DEFAULT_MODE]
    return {
        'mode': m,
        'label': info['label'],
        'desc': info['desc'],
        'modes': [{'id': k, 'label': MODE_INFO[k]['label'],
                   'short': MODE_INFO[k]['short'],
                   'desc': MODE_INFO[k]['desc']} for k in MODES],
        'auto_kinds': sorted(_ALLOW.get(m, frozenset())),
        'can_bypass': m == 'full',
        'warn': (m == 'full'),
    }


def describe():
    """给系统提示词用的一段话。"""
    m = current_mode()
    info = MODE_INFO.get(m) or MODE_INFO[DEFAULT_MODE]
    if m == 'readonly':
        return ('当前是**只读模式**：没有任何写工具，只能查询和回答。'
                '用户要改数据时，说明需要他自己在界面上操作，'
                '或在助手右上角把模式切到「逐条确认」以上。')
    if m == 'ask':
        return ('当前是**逐条确认模式**：所有写入都会生成草稿卡，由用户点「确认写入」'
                '才落库。调用写工具后只需说明要写什么、有无疑点，'
                '绝不要说「已写入」——你并没有写，是用户点确认后才写。')
    if m == 'auto':
        return ('当前是**自动写入模式**：记账、改账、公司/账户/标签/定期/汇率/盘点的'
                '日常维护会直接落库（可按 wid 撤销）；删除流水、批量导入、'
                '删公司或账户仍会生成审核卡，等用户点确认。')
    return ('当前是**完全访问模式**：任何写入都直接执行，不再询问用户。'
            '删除类操作仍要在回复里说清删了什么，方便用户用「撤销这次」还原。')
