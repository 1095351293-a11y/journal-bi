# -*- coding: utf-8 -*-
"""Agent 权限策略 —— 决定哪些操作 AI 可以直接执行、哪些必须人点确认

设计
----
* ``auto``    ：AI 调工具后立刻落库，不再等确认（通过 agent_writes.jsonl 留痕，可按 wid 撤销）
* ``confirm`` ：仍然只生成草稿卡，必须人勾选确认才落库

默认策略（分级放开）
--------------------
新增流水 / 修改流水 / 标签·定期·汇率·盘点·公司·账户的日常维护 → auto
删除流水 / 批量导入对账单 / 删公司·账户·定期·盘点单              → confirm

想调整就改 ``data/agent_perm.json``，不用动代码：
    {"create": "auto", "update": "auto", "delete": "confirm", "import": "confirm"}
把某个值改成 ``auto`` 就放开，改成 ``confirm`` 就收紧。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
PERM_PATH = os.path.join(DATA_DIR, 'agent_perm.json')

# 流水写入的四种类型
DEFAULT_KINDS = {
    'create': 'auto',      # 记一笔
    'update': 'auto',      # 改一笔
    'delete': 'confirm',   # 删流水
    'import': 'confirm',   # 批量导入对账单
}

# 管理类操作（公司/账户/标签/定期/汇率/盘点）
DEFAULT_ADMIN = 'auto'
DEFAULT_ADMIN_DANGER = 'confirm'    # 删除类、会级联删数据的

_CACHE = {'mtime': None, 'data': None}


def _defaults():
    d = dict(DEFAULT_KINDS)
    d['admin'] = DEFAULT_ADMIN
    d['admin_danger'] = DEFAULT_ADMIN_DANGER
    return d


def load():
    """读策略文件；坏文件就当默认，不让配置问题把系统打死。"""
    base = _defaults()
    try:
        mt = os.path.getmtime(PERM_PATH)
    except OSError:
        _CACHE['data'] = base
        _CACHE['mtime'] = None
        return base
    if _CACHE['mtime'] == mt and _CACHE['data']:
        return _CACHE['data']
    try:
        with open(PERM_PATH, encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in base and str(v).lower() in ('auto', 'confirm'):
                    base[k] = str(v).lower()
    except (OSError, ValueError):
        pass
    _CACHE['data'] = base
    _CACHE['mtime'] = mt
    return base


def mode_of(kind):
    """'auto' | 'confirm'"""
    return load().get(kind) or 'confirm'


def is_auto(kind):
    return mode_of(kind) == 'auto'


def save(patch):
    """写策略文件（合并式），给以后的设置页用。"""
    cur = load()
    for k, v in (patch or {}).items():
        if k in cur and str(v).lower() in ('auto', 'confirm'):
            cur[k] = str(v).lower()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = PERM_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PERM_PATH)
        _CACHE['mtime'] = None
    except OSError:
        pass
    return cur


def describe():
    """给系统提示词用的一句话摘要。"""
    m = load()
    can = [k for k in ('create', 'update', 'admin') if m.get(k) == 'auto']
    need = [k for k in ('delete', 'import', 'admin_danger') if m.get(k) != 'auto']
    label = {'create': '记一笔', 'update': '改一笔', 'admin': '公司/账户/标签/定期/汇率/盘点维护',
             'delete': '删除流水', 'import': '批量导入对账单',
             'admin_danger': '删除公司/账户/定期/盘点单'}
    a = '、'.join(label[k] for k in can) or '无'
    b = '、'.join(label[k] for k in need) or '无'
    return f'可直接执行：{a}；需用户点确认：{b}。'
