# -*- coding: utf-8 -*-
"""票据打印的 Agent 工具

把并入后的打印模板接给 Agent：查模板、查票据、填票据、改坐标、从流水生成票据。

三条边界，写之前想清楚：

1. **打印动作不进工具**。浏览器打印对话框必须人来点，服务端静默打印到针式机
   不可控（还会影响用户正常打印）。Agent 能做的最后一公里是给出
   ``#print/<模板id>/<票据序号>`` 深链接，用户点开就是「待打印」状态。
2. **坐标单位为 mm，y 是文字基线**。Agent 改坐标只允许微调（±3mm 内 clamped），
   大改要靠人拖 —— AI 看不出「字压到印刷字上」，只会把数字摆对。
3. **从流水带数据只做「能对上」的字段**。字段名千差万别（收款人/收款單位/beneficiary），
   用关键词打分匹配，对不上的直接留空并回报，绝不猜。
"""
import os
import re
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'journal.db')

import print_tpl as PT  # noqa: E402


# ---------------------------------------------------------------- 内部
def _tpls():
    return PT.load_all()


def _find_tpl(key):
    """按 id / 名称找模板；名称支持包含匹配（唯一时）。"""
    arr = _tpls()
    if not arr:
        return None
    s = str(key or '').strip()
    if not s:
        return arr[0]
    for t in arr:
        if t['id'] == s:
            return t
    for t in arr:
        if t['name'] == s:
            return t
    hit = [t for t in arr if s and s in t['name']]
    return hit[0] if len(hit) == 1 else None


def _brief(t):
    st = t['settings']
    return {
        'id': t['id'], 'name': t['name'],
        'fields': len(t['fields']), 'records': len(t['records']),
        'paper': '%gx%gmm' % (st['w'], st['h']),
        'rot': st['rot'],
        'link': '#print/%s/%d' % (t['id'], t['active']),
    }


def _entry(eid):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute(
            'SELECT e.*, a.name AS acc_name, a.currency AS ccy, a.bank AS bank,'
            ' c.name AS co_name FROM entries e'
            ' LEFT JOIN accounts a ON a.id=e.account_id'
            ' LEFT JOIN companies c ON c.id=a.company_id WHERE e.id=?', (eid,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


# 字段名 → 流水字段的关键词表。顺序即优先级，越靠前越具体。
FIELD_RULES = [
    ('date',   ['日期', 'date', '年月日', '开票日']),
    ('payee',  ['收款人姓名', '收款單位', '收款单位', '收款人', 'payee', 'beneficiary', '受款人']),
    ('party',  ['匯款人', '汇款人', '付款人', 'remitter', 'applicant']),
    ('acc',    ['收款人賬號', '收款人账号', '賬號', '账号', 'account no', 'acct']),
    ('bank',   ['收款銀行名稱', '收款银行', '收款行', '銀行名稱', 'bank name', 'bank']),
    ('swift',  ['swift']),
    ('amount', ['金額', '金额', 'amount', 'amt', '32a']),
    ('memo',   ['附言', '摘要', '用途', 'msg', 'remittance info', 'note', 'purpose', '請款理由', '请款理由']),
]


def _hit(field_name, keys):
    n = (field_name or '').lower()
    for k in keys:
        if k.lower() in n:
            return True
    return False


def _map_entry_to_fields(tpl, e):
    """把一笔流水映射成「字段id → 值」。返回 (values, filled, missing)。"""
    amt = e['credit'] or e['debit'] or 0
    amt_txt = ('%s %s' % (e['ccy'] or '', '{:,.2f}'.format(float(amt)))).strip() \
        if e.get('ccy') else '{:,.2f}'.format(float(amt))
    date_txt = (e.get('date') or '').replace('-', '/')
    src = PT.amt_src(tpl)
    vals, filled = {}, []
    for f in tpl['fields']:
        if f.get('auto'):
            continue                      # 派生字段跟着金额自动算，不单独填
        name = f['name']
        v = ''
        if f.get('kind') == 'check':
            continue
        if f.get('autoSrc'):
            v = amt_txt
        elif _hit(name, FIELD_RULES[0][1]):
            v = date_txt
        elif _hit(name, FIELD_RULES[1][1]):
            v = e.get('party') or ''
        elif _hit(name, FIELD_RULES[2][1]):
            v = e.get('co_name') or e.get('acc_name') or ''
        elif _hit(name, FIELD_RULES[3][1]):
            v = ''                        # 账号在流水里没有，留空让人填
        elif _hit(name, FIELD_RULES[4][1]):
            v = e.get('bank') or ''
        elif _hit(name, FIELD_RULES[5][1]):
            v = ''
        elif _hit(name, FIELD_RULES[6][1]):
            v = amt_txt
        elif _hit(name, FIELD_RULES[7][1]):
            v = e.get('description') or ''
        if v:
            vals[f['id']] = v
            filled.append(name)
    missing = [f['name'] for f in tpl['fields']
               if not f.get('auto') and f.get('kind') != 'check'
               and f['id'] not in vals and not f.get('autoSrc')]
    return vals, filled, missing


def _sanitize_coord(v, old, limit=3.0):
    """Agent 只允许微调：单次 ±3mm，防止把整张票据文字拖飞。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return old
    if abs(f - old) > limit:
        f = old + (limit if f > old else -limit)
    return round(f, 1)


# ---------------------------------------------------------------- 读
def t_list_print_templates(args):
    arr = _tpls()
    if not arr:
        return {'count': 0, 'items': [], 'hint': '还没有模板，可用 create_print_template 新建'}
    return {'count': len(arr), 'items': [_brief(t) for t in arr]}


def t_get_print_template(args):
    t = _find_tpl(args.get('template'))
    if not t:
        return {'error': '找不到模板：%s' % args.get('template'),
                'available': [x['name'] for x in _tpls()]}
    fields = [{'id': f['id'], 'name': f['name'], 'x': f['x'], 'y': f['y'],
               'size': f['size'], 'on': f['on'],
               'type': ('check' if f.get('kind') == 'check' else
                        'amount_src' if f.get('autoSrc') else
                        f.get('auto') or 'text'),
               'value': PT.compute_values(t).get(f['id'], '')}
              for f in t['fields']]
    recs = [{'index': i, 'name': r['name'],
             'link': '#print/%s/%d' % (t['id'], i)}
            for i, r in enumerate(t['records'])]
    return {'id': t['id'], 'name': t['name'], 'paper': _brief(t)['paper'],
            'rot': t['settings']['rot'], 'fields': fields,
            'records': recs, 'auto_amount_field': (PT.amt_src(t) or {}).get('name')}


def t_get_print_record(args):
    t = _find_tpl(args.get('template'))
    if not t:
        return {'error': '找不到模板：%s' % args.get('template')}
    idx = args.get('record')
    idx = int(idx) if isinstance(idx, (int, float)) else (int(idx) if str(idx or '').isdigit() else t['active'])
    if not (0 <= idx < len(t['records'])):
        return {'error': '票据不存在：第 %s 张（共 %d 张）' % (idx, len(t['records']))}
    vals = PT.compute_values(t, idx)
    rows = [{'name': f['name'], 'value': vals.get(f['id'], ''),
             'type': ('check' if f.get('kind') == 'check' else
                      f.get('auto') or ('amount_src' if f.get('autoSrc') else 'text'))}
            for f in t['fields'] if f.get('on')]
    return {'template': t['name'], 'record': t['records'][idx]['name'],
            'index': idx, 'values': rows,
            'link': '#print/%s/%d' % (t['id'], idx),
            'tip': '点 link 打开这套票据的打印视图，Ctrl+P 出纸'}


# ---------------------------------------------------------------- 写
def t_create_print_record(args):
    """新建一张票据并填值。from_entry_id 时自动从流水带出能对上的字段。"""
    t = _find_tpl(args.get('template'))
    if not t:
        return {'error': '找不到模板：%s' % args.get('template'),
                'available': [x['name'] for x in _tpls()]}
    vals = {}
    filled, missing = [], []
    eid = args.get('from_entry_id')
    if eid:
        e = _entry(int(eid))
        if not e:
            return {'error': '流水不存在：%s' % eid}
        vals, filled, missing = _map_entry_to_fields(t, e)
    given = args.get('values') or {}
    got = PT._values_by_name(t, given)
    bad = PT._unknown_names(t, given)
    vals.update(got)
    name = (args.get('name') or '').strip()
    if not name:
        src = t.get('fields') or []
        payee_id = next((f['id'] for f in src if _hit(f['name'], FIELD_RULES[1][1])), None)
        name = (vals.get(payee_id) or ('票据 %d' % (len(t['records']) + 1)))
    rec = {'name': str(name), 'values': vals}
    t['records'].append(rec)
    t['active'] = len(t['records']) - 1
    with PT._lock:
        PT.save(t)
    out = {'ok': True, 'template': t['name'], 'index': t['active'], 'name': rec['name'],
           'link': '#print/%s/%d' % (t['id'], t['active']),
           'values': PT.compute_values(t, t['active'])}
    if filled:
        out['auto_filled'] = filled
    if missing:
        out['need_input'] = missing[:12]
        out['hint'] = '这些字段流水里没有对应数据，需要人工补：' + '、'.join(missing[:8])
    if bad:
        out['unknown_fields'] = bad
    return out


def t_update_print_record(args):
    t = _find_tpl(args.get('template'))
    if not t:
        return {'error': '找不到模板：%s' % args.get('template')}
    idx = args.get('record')
    idx = int(idx) if isinstance(idx, (int, float)) else (int(idx) if str(idx or '').isdigit() else t['active'])
    if not (0 <= idx < len(t['records'])):
        return {'error': '票据不存在：第 %s 张' % idx}
    got = PT._values_by_name(t, args.get('values') or {})
    if not got:
        return {'error': 'values 里没有能对上的字段名',
                'fields': [f['name'] for f in t['fields']]}
    t['records'][idx]['values'].update(got)
    if args.get('name'):
        t['records'][idx]['name'] = str(args['name'])
    with PT._lock:
        PT.save(t)
    return {'ok': True, 'index': idx, 'name': t['records'][idx]['name'],
            'link': '#print/%s/%d' % (t['id'], idx),
            'values': PT.compute_values(t, idx)}


def t_delete_print_record(args):
    t = _find_tpl(args.get('template'))
    if not t:
        return {'error': '找不到模板：%s' % args.get('template')}
    idx = args.get('record')
    idx = int(idx) if isinstance(idx, (int, float)) else (int(idx) if str(idx or '').isdigit() else -1)
    if len(t['records']) <= 1:
        return {'error': '至少保留一张票据'}
    if not (0 <= idx < len(t['records'])):
        return {'error': '票据不存在：第 %s 张（共 %d 张）' % (idx, len(t['records']))}
    rm = t['records'].pop(idx)
    if t['active'] >= len(t['records']):
        t['active'] = len(t['records']) - 1
    with PT._lock:
        PT.save(t)
    return {'ok': True, 'deleted': rm['name'], 'remaining': len(t['records'])}


def t_update_print_field(args):
    """改字段。坐标单次只允许 ±3mm，字号 ±0.5mm —— 防 AI 把版式拖飞。"""
    t = _find_tpl(args.get('template'))
    if not t:
        return {'error': '找不到模板：%s' % args.get('template')}
    f = PT.field_by_key(t, args.get('field'))
    if not f:
        return {'error': '字段不存在：%s' % args.get('field'),
                'fields': [x['name'] for x in t['fields']]}
    before = {k: f[k] for k in ('x', 'y', 'size', 'on')}
    if args.get('x') is not None:
        f['x'] = _sanitize_coord(args['x'], f['x'])
    if args.get('y') is not None:
        f['y'] = _sanitize_coord(args['y'], f['y'])
    if args.get('size') is not None:
        try:
            s = float(args['size'])
            if abs(s - f['size']) > 0.5:
                s = f['size'] + (0.5 if s > f['size'] else -0.5)
            f['size'] = round(max(1.0, min(30.0, s)), 1)
        except (TypeError, ValueError):
            pass
    if args.get('on') is not None:
        f['on'] = bool(args['on'])
    if args.get('rename'):
        f['name'] = str(args['rename'])
    with PT._lock:
        PT.save(t)
    return {'ok': True, 'field': f['name'], 'before': before,
            'after': {k: f[k] for k in ('x', 'y', 'size', 'on')},
            'link': '#print/%s/%d' % (t['id'], t['active']),
            'note': '单次最多挪 3mm；要大幅调位置请人工拖，AI 看不出是否压到印刷字'}


def t_create_print_template(args):
    tpl = {
        'name': args.get('name') or '新模板',
        'bg': '',
        'fields': args.get('fields') or [],
        'records': [{'name': '票据 1', 'values': {}}],
        'settings': args.get('settings') or {},
    }
    t = PT.normalize(tpl)
    t['id'] = PT._gen_id('t')
    with PT._lock:
        PT.save(t)
    return {'ok': True, 'id': t['id'], 'name': t['name'],
            'fields': len(t['fields']), 'link': '#print/%s/0' % t['id'],
            'hint': '底图需要人上传（画布左侧「上传底图」），Agent 没法判断底图对齐'}


def t_delete_print_template(args):
    t = _find_tpl(args.get('template'))
    if not t:
        return {'error': '找不到模板：%s' % args.get('template')}
    if len(_tpls()) <= 1:
        return {'error': '至少保留一套模板'}
    with PT._lock:
        PT.delete(t['id'])
    return {'ok': True, 'deleted': t['name']}


# ---------------------------------------------------------------- 注册
TOOLS = [
    {'type': 'function', 'function': {
        'name': 'list_print_templates',
        'description': '列出所有票据打印模板（支票、汇款单、请款单等），'
                       '含字段数、已有票据数和纸型。问「有哪些模板」「能开哪些单」时用。',
        'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    {'type': 'function', 'function': {
        'name': 'get_print_template',
        'description': '看某套模板的字段清单（名称、坐标、类型、当前值）和票据列表。'
                       '填票据前先调它确认字段名怎么写。',
        'parameters': {'type': 'object', 'properties': {
            'template': {'type': 'string', 'description': '模板 id 或名称，可省（默认第一套）'}},
            'required': []}}},
    {'type': 'function', 'function': {
        'name': 'get_print_record',
        'description': '看某张票据实际会印出来的值（金额大写等自动字段已算好），'
                       '并给出可直接点开的打印链接。',
        'parameters': {'type': 'object', 'properties': {
            'template': {'type': 'string', 'description': '模板 id 或名称'},
            'record': {'type': 'integer', 'description': '第几张，从 0 开始，可省=当前'}},
            'required': []}}},
    {'type': 'function', 'function': {
        'name': 'create_print_record',
        'description': '新建一张票据并填内容。两种用法：'
                       '(1) from_entry_id 传日记账流水 id，自动把日期、收款人、金额、摘要带进去；'
                       '(2) values 传 {字段名: 值}。'
                       '金额大写/小写这类自动字段不用给，系统会算。'
                       '用户说「照这笔支出开一张汇款单」「给这几个人开支票」时用。',
        'parameters': {'type': 'object', 'properties': {
            'template': {'type': 'string', 'description': '模板 id 或名称（如「中银汇款单」）'},
            'from_entry_id': {'type': 'integer', 'description': '日记账流水 id，可省'},
            'values': {'type': 'object', 'description': '{字段名: 值}，字段名照 get_print_template 抄'},
            'name': {'type': 'string', 'description': '票据名（列表里显示），可省'}},
            'required': ['template']}}},
    {'type': 'function', 'function': {
        'name': 'update_print_record',
        'description': '改一张已有票据的字段值。只传要改的字段即可。',
        'parameters': {'type': 'object', 'properties': {
            'template': {'type': 'string'},
            'record': {'type': 'integer', 'description': '第几张，从 0 开始'},
            'values': {'type': 'object', 'description': '{字段名: 新值}'},
            'name': {'type': 'string', 'description': '顺便改票据名，可省'}},
            'required': ['template', 'values']}}},
    {'type': 'function', 'function': {
        'name': 'delete_print_record',
        'description': '删除一张票据（模板至少保留一张）。',
        'parameters': {'type': 'object', 'properties': {
            'template': {'type': 'string'},
            'record': {'type': 'integer', 'description': '第几张，从 0 开始'}},
            'required': ['template', 'record']}}},
    {'type': 'function', 'function': {
        'name': 'update_print_field',
        'description': '微调某个字段：位置（x/y，mm，y 是文字基线）、字号、显示开关、改名。'
                       '一次最多挪 3mm，防止把票据文字拖到印不到的地方。',
        'parameters': {'type': 'object', 'properties': {
            'template': {'type': 'string'},
            'field': {'type': 'string', 'description': '字段名或 id'},
            'x': {'type': 'number'}, 'y': {'type': 'number'},
            'size': {'type': 'number', 'description': '字号 mm'},
            'on': {'type': 'boolean', 'description': '是否打印'},
            'rename': {'type': 'string'}},
            'required': ['template', 'field']}}},
    {'type': 'function', 'function': {
        'name': 'create_print_template',
        'description': '新建一套空白打印模板（纸型、字段坐标由人后续校准底图）。'
                       '一般不建议 AI 新建模板，除非用户明确要求。',
        'parameters': {'type': 'object', 'properties': {
            'name': {'type': 'string'},
            'fields': {'type': 'array', 'description': '[{name,x,y,size,kind?}]，坐标 mm',
                       'items': {'type': 'object'}},
            'settings': {'type': 'object',
                         'description': '{w,h,offx,offy,rot:"0"|"90cw"|"90ccw",showbg}'}},
            'required': ['name']}}},
    {'type': 'function', 'function': {
        'name': 'delete_print_template',
        'description': '删除一整套打印模板（底图、布局、所有票据一起删，不可恢复）。',
        'parameters': {'type': 'object', 'properties': {
            'template': {'type': 'string'}},
            'required': ['template']}}},
]

IMPL = {}
LABELS = {
    'list_print_templates': '查看打印模板',
    'get_print_template': '查看模板字段',
    'get_print_record': '查看票据内容',
    'create_print_record': '开一张票据',
    'update_print_record': '修改票据内容',
    'delete_print_record': '删除票据',
    'update_print_field': '微调票据字段位置',
    'create_print_template': '新建打印模板',
    'delete_print_template': '删除打印模板',
}

# 写工具 → 四档权限里的 kind。delete 在 auto 模式仍要确认，与账本流水一致。
_WRITE_KIND = {
    'create_print_record': 'create',
    'update_print_record': 'update',
    'update_print_field': 'update',
    'create_print_template': 'create',
    'delete_print_record': 'delete',
    'delete_print_template': 'delete',
}


def _approved(args):
    """与 agent_admin 同一套判定：__approved 只有服务端审核卡能带；
    模型自己传 confirm 只在「完全访问」模式下算数。"""
    if args.get('__approved'):
        return True
    if not args.get('confirm'):
        return False
    try:
        from agent_perm import can_bypass
        return bool(can_bypass())
    except Exception:
        return False


def _auto_ok(kind):
    try:
        from agent_perm import is_auto
        return bool(is_auto(kind))
    except Exception:
        return False


def _impact(name, a):
    """给审核卡写一句人话，用户扫一眼就知道会发生什么。"""
    tpl = a.get('template') or '（默认模板）'
    if name == 'create_print_record':
        src = a.get('from_entry_id')
        tail = (' 数据来自流水 #%s' % src) if src else ''
        vals = a.get('values') or {}
        kv = '，'.join('%s=%s' % (k, v) for k, v in list(vals.items())[:4])
        return '在「%s」新建一张票据%s%s' % (tpl, ('：' + kv) if kv else '', tail)
    if name == 'update_print_record':
        vals = a.get('values') or {}
        kv = '，'.join('%s=%s' % (k, v) for k, v in list(vals.items())[:4])
        return '改「%s」第 %s 张票据：%s' % (tpl, a.get('record', '当前'), kv or '名称')
    if name == 'delete_print_record':
        return '删「%s」第 %s 张票据（不可恢复）' % (tpl, a.get('record'))
    if name == 'update_print_field':
        bits = []
        for k, lab in (('x', 'X'), ('y', 'Y'), ('size', '字号')):
            if a.get(k) is not None:
                bits.append('%s→%s' % (lab, a[k]))
        return '调「%s」字段「%s」%s' % (tpl, a.get('field'), '、'.join(bits) or '属性')
    if name == 'create_print_template':
        return '新建一套模板「%s」（%d 个字段）' % (
            a.get('name'), len(a.get('fields') or []))
    if name == 'delete_print_template':
        return '删除整套模板「%s」，底图、布局和全部票据一起删' % tpl
    return LABELS.get(name, name)


def _guard(name, fn):
    """把原始函数包成 (result, links) 二元组，并在这里过权限闸门。

    run_tool 只认二元组；审核卡执行入口 _apply_admin 也按二元组解包，
    所以这一层必须同时满足两边。
    """
    def run(args):
        args = args or {}
        kind = _WRITE_KIND.get(name)
        if kind and not _approved(args) and not _auto_ok(kind):
            try:
                import agent_admin as AD
                d = AD._need_confirm(LABELS.get(name, name), _impact(name, args),
                                     tool=name, args=args,
                                     sid=str(args.get('__sid') or ''),
                                     module='agent_print')
                return d, []
            except Exception as exc:
                return {'error': '需要确认，但审核卡生成失败：%s: %s'
                                 % (type(exc).__name__, exc)}, []
        try:
            r = fn(args)
        except Exception as exc:
            return {'error': '%s: %s' % (type(exc).__name__, exc)}, []
        return r if isinstance(r, tuple) else (r, [])
    return run


for _name, _fn in (
    ('list_print_templates', t_list_print_templates),
    ('get_print_template', t_get_print_template),
    ('get_print_record', t_get_print_record),
    ('create_print_record', t_create_print_record),
    ('update_print_record', t_update_print_record),
    ('delete_print_record', t_delete_print_record),
    ('update_print_field', t_update_print_field),
    ('create_print_template', t_create_print_template),
    ('delete_print_template', t_delete_print_template),
):
    IMPL[_name] = _guard(_name, _fn)
