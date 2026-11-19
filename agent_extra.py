# -*- coding: utf-8 -*-
"""页面能点、但 AI 原来够不着的两个出口

补的是上一轮盘出来的缺口：

* ``export_data``  —— 复用 ``/api/export/excel`` 与 ``/api/export/cash-counts``
  的生成逻辑（那两个函数加起来两百多行，不重写，直接在同一进程里调视图函数），
  产出文件落到 ``data/exports/``，返回一个可点的下载链接。
* ``list_reminders`` —— 对应 ``/api/notes/reminders``：备注非空**或**挂了标签的流水，
  也就是「待跟进清单」。``list_tags`` 只能按标签看，查不出「有备注没标签」的那批。

为什么用 test_request_context 调视图，而不是把导出逻辑抽成纯函数
------------------------------------------------------------
``export_excel`` 有上千行，里面既有 openpyxl 样式又有分页设置，动它有把用户
真实导出搞坏的风险。同一进程里给它造一个 request 上下文再取 Response 的字节，
是改动最小、行为最一致的做法——前端点按钮和 AI 调用走的是同一份代码。

失败一律降级成一句话，不让导出问题把对话打断。
"""
import os
import re
import time
from datetime import datetime
from urllib.parse import quote, unquote, urlencode

from agent_write import _fetch, _opt_int

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
EXPORT_DIR = os.path.join(DATA_DIR, 'exports')
KEEP_DAYS = 7

_APP = None
_VIEWS = {}


def bind_app(app, views):
    """由 app.py 在路由注册完成后调用，把 app 和两个导出视图塞进来。"""
    global _APP, _VIEWS
    _APP = app
    _VIEWS = dict(views or {})


# ---------------------------------------------------------------- 公共
def _money(v):
    return round(float(v or 0), 2)


def _fname_from_cd(cd):
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd or '')
    if m:
        return unquote(m.group(1)).strip()
    m = re.search(r'filename="?([^";]+)"?', cd or '')
    return (m.group(1) if m else '').strip()


def _sweep():
    """顺手清掉 7 天前的导出文件，别让 data/exports 无限长。"""
    try:
        cut = time.time() - KEEP_DAYS * 86400
        for fn in os.listdir(EXPORT_DIR):
            p = os.path.join(EXPORT_DIR, fn)
            if os.path.isfile(p) and os.path.getmtime(p) < cut:
                os.remove(p)
    except OSError:
        pass


def _save(data, fname):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    _sweep()
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe = re.sub(r'[\\/:*?"<>|]', '_', fname or 'export.xlsx').strip(' .')
    name = f'{stamp}_{safe}'
    with open(os.path.join(EXPORT_DIR, name), 'wb') as f:
        f.write(data)
    return name


def _gen(view_key, query):
    """在请求上下文里跑视图函数，拿 (bytes, 文件名)。失败返回 (None, 错误文案)。"""
    if _APP is None or view_key not in _VIEWS:
        return None, None, '导出模块未就绪（服务刚启动时可能还没绑定，稍后重试）'
    try:
        with _APP.test_request_context('/?' + query):
            resp = _VIEWS[view_key]()
            # 视图出错时返回的是 JSON（bad(...)），别把它存成 .xlsx
            if 'json' in (resp.headers.get('Content-Type') or '').lower():
                j = resp.get_json(silent=True) or {}
                return None, None, (j.get('error') or j.get('message')
                                    or f'导出被拒绝（HTTP {resp.status_code}）')
            # send_file 默认 direct_passthrough，get_data() 会抛
            # "Attempted implicit sequence conversion"，先关掉再取字节
            resp.direct_passthrough = False
            data = resp.get_data()
            fname = _fname_from_cd(resp.headers.get('Content-Disposition'))
            if not data:
                return None, None, '导出结果是空的，可能筛选条件没命中任何数据'
            if not fname:
                fname = 'export.xlsx'
            return data, fname, None
    except Exception as e:                       # noqa: BLE001 导出失败不影响对话
        return None, None, f'生成文件时出错：{type(e).__name__}: {e}'


# ---------------------------------------------------------------- 导出
def t_export_data(args):
    kind = str(args.get('kind') or 'journal').strip().lower()
    cid = _opt_int(args.get('company_id'))
    aid = _opt_int(args.get('account_id'))
    frm = str(args.get('date_from') or '').strip()
    to = str(args.get('date_to') or '').strip()
    tag_id = _opt_int(args.get('tag_id'))
    kw = str(args.get('keyword') or '').strip()

    if kind in ('journal', '流水', '流水表'):
        if not aid:
            return {'error': '导出流水表要指定账户：先 list_accounts 拿 account_id，'
                             '或者用 kind=master 导全量'}, []
        q = {'type': 'journal', 'account_id': aid}
        if frm:
            q['from'] = frm
        if to:
            q['to'] = to
        if tag_id:
            q['tag_id'] = tag_id
        if kw:
            q['q'] = kw
        if args.get('group_by_month'):
            q['group'] = 1
        data, fname, err = _gen('excel', urlencode(q))
        label = '流水表'

    elif kind in ('deposits', '定期', '定期存款'):
        if not cid:
            return {'error': '导出定期存款要指定 company_id'}, []
        data, fname, err = _gen('excel', urlencode(
            {'type': 'deposits', 'company_id': cid}))
        label = '定期存款表'

    elif kind in ('cash_counts', '盘点', '现金盘点'):
        if not cid:
            return {'error': '导出盘点表要指定 company_id'}, []
        q = {'company_id': cid}
        if frm:
            q['from'] = frm
        if to:
            q['to'] = to
        data, fname, err = _gen('cash_counts', urlencode(q))
        label = '现金盘点表'

    elif kind in ('master', 'all', '全量', '全量导出'):
        q = {'type': 'master'}
        if frm:
            q['from'] = frm
        if to:
            q['to'] = to
        if cid:
            q['companies'] = cid
        data, fname, err = _gen('excel', urlencode(q))
        label = '全量账套'
    else:
        return {'error': f'不认识的导出类型 {kind}。可用：journal / deposits / '
                         f'cash_counts / master'}, []

    if err:
        return {'error': err,
                'hint': '也可以让用户直接在页面上点「导出 Excel」，两边用的是同一份代码'}, []

    name = _save(data, fname)
    url = '/api/export/agent/' + quote(name)
    return {
        'ok': True,
        'kind': kind,
        'label': label,
        'fname': fname,
        'size_kb': round(len(data) / 1024, 1),
        'download': url,
        'link': 'url:' + url,
        'message': f'{label}已生成：{fname}（{len(data) / 1024:.0f} KB）',
        'note': '把 link 用 show_links 推给用户，或在正文里写成 markdown 链接；'
                '不要自己拼路径',
    }, [{'label': f'下载 {fname}', 'target': 'url:' + url}]


# ---------------------------------------------------------------- 待跟进
def t_list_reminders(args):
    cid = _opt_int(args.get('company_id'))
    tag = str(args.get('tag') or '').strip()
    kw = str(args.get('keyword') or '').strip()
    try:
        limit = int(args.get('limit') or 40)
    except (TypeError, ValueError):
        limit = 40
    limit = max(1, min(limit, 200))

    where = ["(TRIM(COALESCE(e.note,'')) <> '' "
             "OR e.id IN (SELECT entry_id FROM entry_tags))"]
    p = []
    if cid:
        where.append('c.id = ?')
        p.append(cid)
    if tag:
        where.append('e.id IN (SELECT et.entry_id FROM entry_tags et '
                     'JOIN tags t ON t.id=et.tag_id WHERE t.name LIKE ?)')
        p.append(f'%{tag}%')
    if kw:
        where.append('(e.description LIKE ? OR e.note LIKE ? '
                     'OR e.party LIKE ? OR e.check_no LIKE ?)')
        p += [f'%{kw}%'] * 4

    sql = ('SELECT e.id, e.account_id, e.date, e.description, e.note, e.party, '
           'e.project, e.debit, e.credit, e.check_no, e.pending, '
           'a.name AS account_name, a.currency, '
           'c.id AS company_id, c.name AS company_name, '
           "(SELECT GROUP_CONCAT(t.name, '|') FROM entry_tags et "
           '  JOIN tags t ON t.id = et.tag_id WHERE et.entry_id = e.id) AS tag_names '
           'FROM entries e JOIN accounts a ON a.id = e.account_id '
           'JOIN companies c ON c.id = a.company_id '
           'WHERE ' + ' AND '.join(where) +
           ' ORDER BY e.date DESC, e.id DESC LIMIT ?')
    p.append(limit)
    rr = _fetch(sql, p)

    cnt = _fetch('SELECT COUNT(*) AS n FROM entries e '
                 'JOIN accounts a ON a.id = e.account_id '
                 'JOIN companies c ON c.id = a.company_id '
                 'WHERE ' + ' AND '.join(where), p[:-1])
    total = (cnt[0]['n'] if cnt else 0)

    links, out, only_note, only_tag = [], [], 0, 0
    for r in rr:
        tags = [x for x in str(r.get('tag_names') or '').split('|') if x]
        has_note = bool((r.get('note') or '').strip())
        if has_note and not tags:
            only_note += 1
        if tags and not has_note:
            only_tag += 1
        target = f"entry:{r['id']}:{r['account_id']}"
        out.append({
            'entry_id': r['id'], 'date': r['date'], 'company': r['company_name'],
            'account': r['account_name'], 'currency': r['currency'],
            'description': r['description'], 'party': r['party'],
            'note': r.get('note') or '', 'tags': tags,
            'debit_in': _money(r['debit']), 'credit_out': _money(r['credit']),
            'pending': bool(r['pending']), 'link': target,
        })
        if len(links) < 5:
            amt = (f"收 {_money(r['debit']):,.2f}" if r['debit']
                   else f"支 {_money(r['credit']):,.2f}")
            links.append({
                'label': f"{r['date']} {r['description'] or '流水'} · {amt} "
                         f"{r['currency']} · {'、'.join(tags) or '备注'}",
                'target': target,
            })

    res = {'total': total, 'returned': len(out), 'items': out,
           'breakdown': {'只有备注': only_note, '只有标签': only_tag}}
    if total > len(out):
        res['hint'] = f'共 {total} 笔待跟进，这里列出最近 {len(out)} 笔'
    if not total:
        res['hint'] = '当前没有挂着备注或标签的流水'
    return res, links


def t_delete_attachment(args):
    aid = _opt_int(args.get('att_id'))
    if not aid:
        return {'error': '要给 att_id。不确定就先 list_attachments 查'}, []
    rr = _fetch('SELECT at.*, e.date, e.description FROM attachments at '
                'JOIN entries e ON e.id = at.entry_id WHERE at.id = ?', (aid,))
    if not rr:
        return {'error': f'没有 id={aid} 的附件'}, []
    a = rr[0]
    who = f"{a['date']} {a['description'] or '流水'}"
    try:
        from agent_admin import _approved, _need_confirm
    except Exception:
        _approved, _need_confirm = (lambda x: False), None
    if not _approved(args):
        impact = (f"将永久删除附件「{a['orig_name']}」（挂在 {who} 上），"
                  f"文件删掉后无法恢复，流水本身不受影响")
        if _need_confirm:
            return _need_confirm('删除附件', impact, 'delete_attachment',
                                 args, str(args.get('__sid') or ''),
                                 module='agent_extra'), []
        return {'need_confirm': True, 'att_id': aid, 'impact': impact,
                'hint': '把影响告给用户，等他在界面上确认'}, []
    if _APP is None or 'del_attachment' not in _VIEWS:
        return {'error': '删除模块未就绪'}, []
    try:
        with _APP.test_request_context('/api/attachments/%d' % aid, method='DELETE'):
            resp = _VIEWS['del_attachment'](aid)
        if resp.status_code >= 400:
            j = resp.get_json(silent=True) or {}
            return {'error': j.get('error') or f'删除失败（HTTP {resp.status_code}）'}, []
    except Exception as e:                       # noqa: BLE001
        return {'error': f'删除时出错：{type(e).__name__}: {e}'}, []
    return {'ok': True, 'att_id': aid, 'fname': a['orig_name'],
            'message': f"已删除附件「{a['orig_name']}」"}, []


def t_list_attachments(args):
    aid = _opt_int(args.get('att_id'))
    eid = _opt_int(args.get('entry_id'))
    cid = _opt_int(args.get('company_id'))
    where, p = [], []
    if aid:
        where.append('at.id = ?')
        p.append(aid)
    if eid:
        where.append('at.entry_id = ?')
        p.append(eid)
    if cid:
        where.append('a.company_id = ?')
        p.append(cid)
    if not where:
        return {'error': '给 entry_id（某笔流水的附件）、company_id 或 att_id'}, []
    try:
        limit = max(1, min(int(args.get('limit') or 30), 200))
    except (TypeError, ValueError):
        limit = 30
    sql = ('SELECT at.id, at.entry_id, at.orig_name, at.size, at.mime, at.uploaded_at, '
           'e.date, e.description, e.account_id, a.name AS account_name, '
           'c.name AS company_name '
           'FROM attachments at JOIN entries e ON e.id = at.entry_id '
           'JOIN accounts a ON a.id = e.account_id '
           'JOIN companies c ON c.id = a.company_id '
           'WHERE ' + ' AND '.join(where) + ' ORDER BY at.id DESC LIMIT ?')
    p.append(limit)
    rr = _fetch(sql, p)
    out, links = [], []
    for r in rr:
        out.append({'att_id': r['id'], 'entry_id': r['entry_id'],
                    'fname': r['orig_name'],
                    'size_kb': round((r['size'] or 0) / 1024, 1),
                    'mime': r['mime'], 'uploaded_at': r['uploaded_at'],
                    'date': r['date'], 'description': r['description'],
                    'account': r['account_name'], 'company': r['company_name'],
                    'link': f"attachment:{r['id']}"})
        if len(links) < 5:
            links.append({'label': f"{r['orig_name']} · {r['date']}",
                          'target': f"attachment:{r['id']}"})
    res = {'ok': True, 'count': len(out), 'items': out}
    if not out:
        res['message'] = '这个范围内没有附件'
    return res, links


TOOLS = [
    {'type': 'function', 'function': {
        'name': 'export_data',
        'description': '生成 Excel 文件并给出下载链接。用户说「导出」「下载表格」'
                       '「给我一份 Excel」「导出来发我」时用，不要再让他去点页面。'
                       '四种类型：'
                       'journal=单个账户的流水表（需 account_id，可带日期/标签/关键词）；'
                       'deposits=定期存款表（需 company_id）；'
                       'cash_counts=现金盘点表（需 company_id）；'
                       'master=全量账套（每公司一页，多公司自动打包 zip，可带日期）。'
                       '生成后把返回的 link 用 show_links 推给用户。',
        'parameters': {'type': 'object', 'properties': {
            'kind': {'type': 'string',
                     'enum': ['journal', 'deposits', 'cash_counts', 'master'],
                     'description': '导出类型，默认 journal'},
            'account_id': {'type': 'integer',
                           'description': 'journal 必填，先 list_accounts 拿'},
            'company_id': {'type': 'integer',
                           'description': 'deposits / cash_counts 必填；master 可选（只导这家）'},
            'date_from': {'type': 'string', 'description': 'YYYY-MM-DD，可省'},
            'date_to': {'type': 'string', 'description': 'YYYY-MM-DD，可省'},
            'tag_id': {'type': 'integer', 'description': '只导带某标签的流水'},
            'keyword': {'type': 'string', 'description': '只导摘要/对方命中关键词的流水'},
            'group_by_month': {'type': 'boolean',
                               'description': '流水表按月分组小计，默认 false'}},
            'required': ['kind']}}},
    {'type': 'function', 'function': {
        'name': 'list_reminders',
        'description': '待跟进清单：所有**有备注**或**挂了标签**的流水，也就是界面右侧'
                       '提醒弹窗里那一批（未收收据、未签字、待核对这类）。'
                       '用户问「有哪些还没跟进」「这周要处理什么」「哪些没签收」时用它，'
                       '比 list_tags 更全——list_tags 查不出只有备注、没挂标签的那些。',
        'parameters': {'type': 'object', 'properties': {
            'company_id': {'type': 'integer', 'description': '只查这家公司，可省'},
            'tag': {'type': 'string', 'description': '只看某个标签，可省'},
            'keyword': {'type': 'string', 'description': '摘要/备注/对方/票号关键词'},
            'limit': {'type': 'integer', 'description': '默认 40，最大 200'}},
            'required': []}}},
    {'type': 'function', 'function': {
        'name': 'list_attachments',
        'description': '查附件：某笔流水挂了哪些文件、某家公司总共有哪些。'
                       '用户问「这笔的发票呢」「附件还在不在」时用；'
                       '删之前也要先用它确认 att_id。',
        'parameters': {'type': 'object', 'properties': {
            'entry_id': {'type': 'integer', 'description': '只查这一笔流水'},
            'company_id': {'type': 'integer', 'description': '只查这家公司'},
            'att_id': {'type': 'integer', 'description': '只查某个附件'},
            'limit': {'type': 'integer', 'description': '默认 30，最大 200'}},
            'required': []}}},
    {'type': 'function', 'function': {
        'name': 'delete_attachment',
        'description': '删除一个附件（文件和记录一起删，不可恢复，流水本身不受影响）。'
                       '两段式：第一次不带 confirm 会返回 impact，'
                       '把影响告给用户、他明确同意后再带 confirm=true 重调。'
                       '**不要**自己判断「重复了」就直接删。',
        'parameters': {'type': 'object', 'properties': {
            'att_id': {'type': 'integer', 'description': '要删的附件 id'},
            'confirm': {'type': 'boolean',
                        'description': '用户已明确同意时才传 true'}},
            'required': ['att_id']}}},
]

IMPL = {'export_data': t_export_data, 'list_reminders': t_list_reminders,
        'list_attachments': t_list_attachments,
        'delete_attachment': t_delete_attachment}
LABELS = {'export_data': '导出 Excel', 'list_reminders': '读取待跟进清单',
          'list_attachments': '查看附件', 'delete_attachment': '删除附件'}
