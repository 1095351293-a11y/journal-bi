# -*- coding: utf-8 -*-
"""Skill —— 把「反复要做的一串动作」存成一个可以再次执行的技能

为什么是声明式 JSON 而不是让 AI 写 Python
----------------------------------------
AI 写的代码直接跑在这台机器上，能 import sqlite3 就能绕开 agent_perm 和 undo，
一次幻觉就能把 1765 笔流水改坏。这里给的是一条窄通道：**skill 只能按顺序调
已注册的工具，不能碰数据库、不能读文件、不能起进程**。

能表达什么：顺序调用 + 参数替换 + 结果引用。
表达不了 if/for/算数——那种需求应该直接对话，不要塞进 skill。

存放
----
``data/skills/<id>.json``，和 agent_perm.json 同级。属于用户数据，不入库。

结构
----
{
  "id": "month_end_check",
  "name": "月末结账体检",
  "desc": "查未兑现、待跟进、余额是否与盘点对得上",
  "when": "用户说月末体检 / 结账前检查 / 这个月有没有漏",
  "params": {"company_id": {"type": "integer", "required": true}},
  "steps": [
    {"tool": "query_entries", "args": {"company_id": "${company_id}",
                                       "status": "pending"}, "as": "pending"},
    {"tool": "list_reminders", "args": {"company_id": "${company_id}"}, "as": "todo"}
  ]
}

参数写法：``${name}`` 占位，整串就是一个占位符时保留原类型（int 还是 int）。
上一步的结果可以用 ``${as名.字段}`` 取，例如 ``${pending.total}``。
"""
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
SKILL_DIR = os.path.join(DATA_DIR, 'skills')

MAX_STEPS = 10
MAX_STEPS_IN_PROMPT = 12
MAX_RESULT = 3000          # 单步结果截断，别把 token 撑爆
# 不允许被 skill 调用的工具：会造成递归或不可预期的连锁
BLOCKED = {'create_skill', 'run_skill', 'delete_skill', 'list_skills'}


# ---------------------------------------------------------------- 存取
def _path(sid):
    sid = re.sub(r'[^A-Za-z0-9_.-]', '_', str(sid or '').strip())[:60]
    return os.path.join(SKILL_DIR, sid + '.json')


def load(sid):
    try:
        with open(_path(sid), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get('id') else None
    except (OSError, ValueError):
        return None


def all_skills():
    out = []
    try:
        for fn in sorted(os.listdir(SKILL_DIR)):
            if not fn.endswith('.json'):
                continue
            try:
                with open(os.path.join(SKILL_DIR, fn), encoding='utf-8') as f:
                    d = json.load(f)
                if isinstance(d, dict) and d.get('id'):
                    out.append(d)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out


def save(sk):
    os.makedirs(SKILL_DIR, exist_ok=True)
    p = _path(sk['id'])
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(sk, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return sk


def remove(sid):
    p = _path(sid)
    if os.path.exists(p):
        os.remove(p)
        return True
    return False


# ---------------------------------------------------------------- 参数替换
_RE_FULL = re.compile(r'^\$\{([A-Za-z0-9_.]+)\}$')
_RE_ANY = re.compile(r'\$\{([A-Za-z0-9_.]+)\}')


def _lookup(ctx, path):
    cur = ctx
    for seg in path.split('.'):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.isdigit():
            cur = cur[int(seg)] if int(seg) < len(cur) else None
        else:
            return None
    return cur


def _subst(v, ctx):
    if isinstance(v, str):
        m = _RE_FULL.match(v.strip())
        if m:
            got = _lookup(ctx, m.group(1))
            return v if got is None else got
        return _RE_ANY.sub(lambda x: ('' if _lookup(ctx, x.group(1)) is None
                                      else str(_lookup(ctx, x.group(1)))), v)
    if isinstance(v, dict):
        return {k: _subst(x, ctx) for k, x in v.items()}
    if isinstance(v, list):
        return [_subst(x, ctx) for x in v]
    return v


def _digest(res):
    """给模型看的单步摘要，太长就截断。"""
    try:
        s = json.dumps(res, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(res)
    return s[:MAX_RESULT]


# ---------------------------------------------------------------- 执行
def run(sk, params, sid=''):
    from agent import TOOL_IMPL            # 延迟导入，避免循环
    ctx = dict(sk.get('defaults') or {})
    ctx.update(params or {})
    declared = sk.get('params') or {}
    missing = [k for k, v in declared.items()
               if isinstance(v, dict) and v.get('required') and k not in ctx]
    if missing:
        need = '、'.join(f'{k}（{declared[k].get("desc") or declared[k].get("type", "值")}）'
                        for k in missing)
        return {'error': f'这个技能需要参数：{need}',
                'skill': sk.get('id'), 'params': declared}, []

    steps, links = sk.get('steps') or [], []
    if not steps:
        return {'error': '这个技能没有步骤'}, []
    out = []
    for i, st in enumerate(steps[:MAX_STEPS], 1):
        tool = str(st.get('tool') or '').strip()
        alias = str(st.get('as') or f's{i}')
        if not tool or tool in BLOCKED:
            out.append({'step': i, 'tool': tool, 'skipped': '不允许在技能里调用'})
            continue
        impl = TOOL_IMPL.get(tool)
        if not impl:
            out.append({'step': i, 'tool': tool, 'error': '没有这个工具'})
            continue
        a = {k: v for k, v in _subst(st.get('args') or {}, ctx).items()
             if not str(k).startswith('__')}
        if sid:
            a['__sid'] = sid
        try:
            res, ls = impl(a)
        except Exception as e:                       # noqa: BLE001
            out.append({'step': i, 'tool': tool,
                        'error': f'{type(e).__name__}: {e}'})
            continue
        ctx[alias] = res
        for l in (ls or [])[:3]:
            if l not in links and len(links) < 5:
                links.append(l)
        note = st.get('label') or tool
        out.append({'step': i, 'label': note, 'tool': tool,
                    'ok': not (isinstance(res, dict) and res.get('error')),
                    'result': _digest(res)})
    return {'ok': True, 'skill': sk.get('id'), 'name': sk.get('name'),
            'steps': out, 'note': '按步骤汇报结果；哪一步出错就单独说，别把整段说成成功'}, links


# ---------------------------------------------------------------- 工具
def t_create_skill(args):
    sid = str(args.get('id') or '').strip()
    name = str(args.get('name') or '').strip()
    if not sid or not name:
        return {'error': 'id 和 name 都要给。id 用英文小写加下划线，如 month_end_check'}, []
    if re.search(r'[^A-Za-z0-9_.-]', sid):
        return {'error': 'id 只能用英文字母、数字、下划线'}, []
    steps = args.get('steps')
    if not isinstance(steps, list) or not steps:
        return {'error': 'steps 要给出步骤数组，每步至少 {tool, args}'}, []
    clean = []
    for st in steps[:MAX_STEPS]:
        if not isinstance(st, dict) or not st.get('tool'):
            continue
        clean.append({'tool': str(st['tool']).strip(),
                      'args': st.get('args') if isinstance(st.get('args'), dict) else {},
                      'as': str(st.get('as') or '').strip() or None,
                      'label': str(st.get('label') or '').strip() or None})
    if not clean:
        return {'error': '没有一个有效的步骤'}, []

    from agent import TOOL_IMPL
    bad = [s['tool'] for s in clean if s['tool'] not in TOOL_IMPL or s['tool'] in BLOCKED]
    if bad:
        return {'error': f'这些工具不存在或不能在技能里用：{", ".join(bad)}。'
                         f'可用工具见工具列表', 'available': sorted(TOOL_IMPL)}, []

    sk = {'id': sid, 'name': name,
          'desc': str(args.get('desc') or '').strip()[:300],
          'when': str(args.get('when') or '').strip()[:200],
          'params': args.get('params') if isinstance(args.get('params'), dict) else {},
          'steps': [{k: v for k, v in s.items() if v} for s in clean]}
    save(sk)
    total = len(all_skills())
    return {'ok': True, 'id': sid, 'name': name, 'steps': len(clean),
            'count': total,
            'message': f'技能「{name}」已保存（{len(clean)} 步）。'
                       f'以后用户提到相关需求，直接 run_skill(id="{sid}")'}, []


def t_run_skill(args):
    sid = str(args.get('__sid') or '')
    key = str(args.get('id') or args.get('name') or '').strip()
    if not key:
        names = [{'id': s['id'], 'name': s.get('name')} for s in all_skills()]
        return {'error': '要用哪个技能？', 'available': names}, []
    sk = load(key)
    if not sk:                          # 允许按中文名匹配
        for s in all_skills():
            if s.get('name') == key or s.get('id') == key:
                sk = s
                break
    if not sk:
        return {'error': f'没有叫 {key} 的技能',
                'available': [{'id': s['id'], 'name': s.get('name')}
                              for s in all_skills()]}, []
    params = args.get('params') if isinstance(args.get('params'), dict) else {}
    for k, v in (args or {}).items():
        if k not in ('id', 'name', 'params', '__sid') and v is not None:
            params.setdefault(k, v)
    return run(sk, params, sid)


def t_list_skills(args):
    items = all_skills()
    if not items:
        return {'ok': True, 'count': 0, 'items': [],
                'message': '还没有技能。用户说「以后都这样做」时可以 create_skill 存一个'}, []
    out = [{'id': s['id'], 'name': s.get('name'), 'when': s.get('when'),
            'desc': s.get('desc'), 'steps': len(s.get('steps') or [])}
           for s in items]
    return {'ok': True, 'count': len(out), 'items': out}, []


def t_delete_skill(args):
    sid = str(args.get('id') or '').strip()
    if not sid:
        return {'error': '要给 id'}, []
    if not remove(sid):
        return {'error': f'没有叫 {sid} 的技能'}, []
    return {'ok': True, 'removed': sid, 'message': f'已删除技能 {sid}'}, []


def block(limit=MAX_STEPS_IN_PROMPT):
    """注入系统提示词：只给 id / 名称 / 什么时候用，不给完整步骤。"""
    items = all_skills()
    if not items:
        return ''
    lines = ['', '## 你已有的技能（用 run_skill 直接执行）']
    for s in items[:limit]:
        when = s.get('when') or s.get('desc') or ''
        lines.append(f"- `{s['id']}` {s.get('name')}：{when}")
    if len(items) > limit:
        lines.append(f"- 还有 {len(items) - limit} 个，用 list_skills 查看")
    lines.append('用户的需求对得上某个技能时，**直接 run_skill**，不要自己一步步重来；'
                 '技能返回的步骤结果照实汇总。')
    return '\n'.join(lines)


TOOLS = [
    {'type': 'function', 'function': {
        'name': 'create_skill',
        'description': '把一串固定动作存成技能，以后一句话就能重跑。'
                       '什么时候用：(1) 用户说「以后都这样做」「存成一个流程」；'
                       '(2) 同一套查询这次已经做过一遍、明显还会反复做。'
                       'skill 只能按顺序调已注册的工具，不能写代码、不能算数。'
                       '有 if/循环/计算的需求不要做成技能，直接对话。',
        'parameters': {'type': 'object', 'properties': {
            'id': {'type': 'string', 'description': '英文小写下划线，如 month_end_check'},
            'name': {'type': 'string', 'description': '中文名，如「月末结账体检」'},
            'desc': {'type': 'string', 'description': '一句话说明它做什么'},
            'when': {'type': 'string',
                     'description': '什么时候该用它，写用户可能说的原话，越具体越好'},
            'params': {'type': 'object',
                       'description': '参数声明，如 {"company_id":{"type":"integer",'
                                      '"required":true,"desc":"公司 id"}}'},
            'steps': {'type': 'array', 'description': '按顺序执行的步骤', 'items': {
                'type': 'object', 'properties': {
                    'tool': {'type': 'string', 'description': '工具名'},
                    'args': {'type': 'object',
                             'description': '参数，可用 ${参数名} 占位；'
                                            '引用前面步骤结果写 ${步骤as名.字段}'},
                    'as': {'type': 'string', 'description': '给这步结果起的名字，供后面引用'},
                    'label': {'type': 'string', 'description': '这一步在汇报里叫什么'}},
                'required': ['tool']}}},
            'required': ['id', 'name', 'steps']}}},
    {'type': 'function', 'function': {
        'name': 'run_skill',
        'description': '执行一个已保存的技能。用户的需求对得上技能的 when 时直接调用，'
                       '不要自己一步步重来。',
        'parameters': {'type': 'object', 'properties': {
            'id': {'type': 'string', 'description': '技能 id 或中文名'},
            'params': {'type': 'object', 'description': '技能需要的参数'}},
            'required': ['id']}}},
    {'type': 'function', 'function': {
        'name': 'list_skills',
        'description': '列出所有已保存的技能。用户问「你有哪些技能」时用。',
        'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    {'type': 'function', 'function': {
        'name': 'delete_skill',
        'description': '删掉一个不再需要的技能。',
        'parameters': {'type': 'object', 'properties': {
            'id': {'type': 'string'}}, 'required': ['id']}}},
]

IMPL = {'create_skill': t_create_skill, 'run_skill': t_run_skill,
        'list_skills': t_list_skills, 'delete_skill': t_delete_skill}
LABELS = {'create_skill': '创建技能', 'run_skill': '执行技能',
          'list_skills': '查看技能', 'delete_skill': '删除技能'}
