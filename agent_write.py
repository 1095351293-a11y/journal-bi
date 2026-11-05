# -*- coding: utf-8 -*-
"""Agent 写入层 —— 只产草稿，用户点确认才落库

设计约束
--------
1. 暴露给模型的工具全部以 ``propose_`` / ``import_`` 开头，函数体内
   **绝不写库**，只把「打算怎么改」放进 PROPOSALS 队列，回一个摘要。
2. 真正的 INSERT/UPDATE/DELETE 只发生在 HTTP 确认端点里，复用
   app.py 的 ``_write_lock`` 与 ``validate_entry_payload``，校验口径与
   手工录入完全一致。
3. 每次写入记一行 ``agent_writes.jsonl``，可按 wid 撤销；新增的撤销是
   删掉、删除的撤销是按原 id 插回、修改的撤销是写回旧值。
4. 删除属高危：草稿带 ``danger`` 标记，前端强制勾选「我确认」才能执行。
"""
import csv
import io
import json
import os
import re
import threading
import time
import uuid
from datetime import date as _date
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'journal.db')
PROP_PATH = os.path.join(DATA_DIR, 'agent_proposals.json')
WRITE_LOG = os.path.join(DATA_DIR, 'agent_writes.jsonl')

MAX_ITEMS = 200           # 单张草稿最多几行，防止一次刷爆
MAX_DELETE = 20           # 单次删除上限
BIG_AMOUNT = 1000000      # 超过这个金额提醒核对
PROP_TTL = 6 * 3600       # 草稿 6 小时后作废

_lock = threading.Lock()
PROPOSALS = {}
_loaded = False

bp = Blueprint('agent_write', __name__)


# ---------------------------------------------------------------- 小工具
def _num(v, default=0.0):
    """金额解析：容忍 1,234.00 / MOP 1,234 / (500) 这类写法。"""
    if v is None or v == '':
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    neg = s.startswith('(') and s.endswith(')')
    s = re.sub(r'[^0-9.\-]', '', s)
    if s in ('', '-', '.'):
        return default
    try:
        n = float(s)
    except ValueError:
        return default
    return -n if neg else n


def _opt_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


_DATE_PATS = [
    (r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', 'ymd'),
    (r'^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})', 'dmy'),
    (r'^(\d{4})(\d{2})(\d{2})$', 'ymd'),
]


def _parse_date(s, default_year=None):
    """把各种日期写法归一成 YYYY-MM-DD；认不出返回 None。"""
    if isinstance(s, _date):
        return s.isoformat()
    t = str(s or '').strip()
    if not t:
        return None
    for pat, mode in _DATE_PATS:
        m = re.match(pat, t)
        if not m:
            continue
        a, b, c = m.groups()
        if mode == 'ymd':
            y, mo, d = int(a), int(b), int(c)
        else:
            d, mo, y = int(a), int(b), int(c)
        try:
            return _date(y, mo, d).isoformat()
        except ValueError:
            return None
    # 「10月28日」这种，补 default_year
    m = re.match(r'^(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日?$', t)
    if m:
        y = int(m.group(1) or default_year or datetime.now().year)
        try:
            return _date(y, int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    return None


def _wdb():
    """拿 app 的请求级连接，保证与页面写入用同一把锁、同一套校验。"""
    import app as A
    return A


def _fetch(sql, params=()):
    import sqlite3
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _today():
    return datetime.now().strftime('%Y-%m-%d')


# ---------------------------------------------------------------- 草稿队列
def _load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(PROP_PATH, encoding='utf-8') as f:
            raw = json.load(f)
        now = time.time()
        for pid, p in (raw or {}).items():
            if p.get('status') == 'pending' and now - p.get('ts', 0) < PROP_TTL:
                PROPOSALS[pid] = p
    except (OSError, ValueError):
        pass


def _save():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        pend = {k: v for k, v in PROPOSALS.items() if v.get('status') == 'pending'}
        # 只留最近 40 张，别把文件撑大
        if len(pend) > 40:
            for k in sorted(pend, key=lambda k: pend[k].get('ts', 0))[:-40]:
                pend.pop(k, None)
        tmp = PROP_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(pend, f, ensure_ascii=False)
        os.replace(tmp, PROP_PATH)
    except OSError:
        pass


def add_proposal(sid, kind, title, items, warnings=None, danger=False):
    _load()
    pid = 'pw' + uuid.uuid4().hex[:10]
    p = {'pid': pid, 'sid': sid or '', 'kind': kind, 'title': title,
         'items': items, 'warnings': warnings or [], 'danger': bool(danger),
         'status': 'pending', 'ts': time.time(),
         'at': datetime.now().strftime('%H:%M')}
    with _lock:
        PROPOSALS[pid] = p
        _save()
    return p


def get_proposal(pid):
    _load()
    with _lock:
        return PROPOSALS.get(pid)


# ---------------------------------------------------------------- 账户解析
def _companies():
    return _fetch('SELECT id, name FROM companies ORDER BY sort, id')


def _accounts():
    return _fetch('SELECT id, company_id, name, currency, bank, account_no, kind '
                  'FROM accounts ORDER BY company_id, sort, id')


def resolve_account(args):
    """按 id / 名称 / 账号 / 币种 + 公司名定位账户，返回 (acc, err)。"""
    accs = _accounts()
    if not accs:
        return None, '账本里还没有账户，请先到「公司与账户」建一个'
    aid = _opt_int(args.get('account_id'))
    if aid:
        for a in accs:
            if a['id'] == aid:
                return a, None
        return None, f'账户 id={aid} 不存在'
    kw = str(args.get('account') or '').strip()
    comp = str(args.get('company') or '').strip()
    cand = accs
    if comp:
        cs = [c for c in _companies()
              if comp in (c['name'] or '') or (c['name'] or '') in comp
              or str(c['id']) == comp]
        if cs:
            cid = cs[0]['id']
            cand = [a for a in accs if a['company_id'] == cid]
        else:
            return None, (f'找不到公司「{comp}」。可选：'
                          + '、'.join(c['name'] for c in _companies()[:8]))
    if not kw:
        if len(cand) == 1:
            return cand[0], None
        return None, ('要指定账户，可选：'
                      + '、'.join(f"{a['name']}({a['currency']})" for a in cand[:10]))
    # 精确 → 包含 → 账号包含
    for a in cand:
        if kw == a['name']:
            return a, None
    hits = [a for a in cand if kw in (a['name'] or '') or
            (a['account_no'] and kw in a['account_no'])]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, ('有多个账户都对得上，请说清楚是哪个：'
                      + '、'.join(f"{a['name']}({a['currency']})" for a in hits[:8]))
    return None, ('找不到账户「' + kw + '」，可选：'
                  + '、'.join(f"{a['name']}({a['currency']})" for a in cand[:10]))


def resolve_tags(names):
    """标签名 → id；不存在的先不建，留给执行阶段建。"""
    if not names:
        return [], []
    out_ids, out_names = [], []
    all_t = _fetch('SELECT id, name FROM tags')
    for raw in names:
        n = str(raw or '').strip().lstrip('#')
        if not n:
            continue
        hit = next((t for t in all_t if t['name'] == n), None)
        if hit:
            out_ids.append(hit['id'])
        out_names.append(n)
    return out_ids, out_names


# ---------------------------------------------------------------- 重复检测
def dup_hits(account_id, d, debit, credit, exclude_id=None):
    """同账户、同日 ±3 天、同金额同方向 → 疑似重复。"""
    if not (debit or credit) or not d:
        return []
    try:
        base = _date.fromisoformat(_parse_date(d) or d)
    except (ValueError, TypeError):
        return []
    lo = (base - timedelta(days=3)).isoformat()
    hi = (base + timedelta(days=3)).isoformat()
    # 只比非零的那一侧，否则 ABS(debit-0) 会把所有同方向记录都算成重复
    sql = ('SELECT id, date, description, party, debit, credit FROM entries '
           'WHERE account_id=? AND date BETWEEN ? AND ? AND ('
           '(? > 0 AND ABS(debit-?) < 0.005) OR (? > 0 AND ABS(credit-?) < 0.005))')
    hit = _fetch(sql, (account_id, lo, hi, debit, debit, credit, credit))
    return [h for h in hit if h['id'] != exclude_id]


# ---------------------------------------------------------------- 构造草稿
def _build_item(args, idx=0, default_year=None, force_account=None):
    """把模型给的字段规范成一条待写入流水，返回 (item, warning 列表, err)。"""
    warns = []
    acc = force_account
    if acc is None:
        acc, err = resolve_account(args)
        if err:
            return None, warns, err
    d = _parse_date(args.get('date'), default_year)
    if not d:
        return None, warns, (f'第 {idx + 1} 行日期认不出来：{args.get("date")!r}，'
                             f'请用 YYYY-MM-DD')
    amount = abs(_num(args.get('amount')))
    debit = abs(_num(args.get('debit')))
    credit = abs(_num(args.get('credit')))
    direction = str(args.get('direction') or '').strip().lower()
    if amount:
        if direction in ('in', '收入', '收', 'income', 'credit', '借', 'debit'):
            debit, credit = amount, 0.0
        elif direction in ('out', '支出', '付', 'expense', 'debit_', '贷'):
            debit, credit = 0.0, amount
        else:
            credit = amount
            warns.append('没说明收支方向，按「支出」处理——如果其实是收入，请在卡片里改')
    if debit and credit:
        return None, warns, f'第 {idx + 1} 行收入与支出只能填一项'
    if not (debit or credit):
        return None, warns, f'第 {idx + 1} 行金额为 0 或没填金额'
    if debit > BIG_AMOUNT or credit > BIG_AMOUNT:
        warns.append(f'第 {idx + 1} 行金额较大（{debit or credit:,.2f}），请重点核对')
    if d > _today():
        warns.append(f'第 {idx + 1} 行日期 {d} 在未来')
    tag_ids, tag_names = resolve_tags(args.get('tags') or args.get('tag_names'))
    item = {
        'account_id': acc['id'],
        'account_name': acc['name'],
        'currency': acc['currency'],
        'company_id': acc['company_id'],
        'date': d,
        'description': str(args.get('description') or '').strip(),
        'party': str(args.get('party') or '').strip(),
        'project': str(args.get('project') or '').strip(),
        'note': str(args.get('note') or '').strip(),
        'check_no': str(args.get('check_no') or '').strip(),
        'debit': round(debit, 2),
        'credit': round(credit, 2),
        'pending': 1 if args.get('pending') else 0,
        'tag_ids': tag_ids, 'tag_names': tag_names,
    }
    for h in dup_hits(acc['id'], d, debit, credit):
        warns.append(f'疑似重复：{h["date"]} 已有 '
                     f'{h["debit"] or h["credit"]:,.2f} · {(h["description"] or "")[:24]}')
    if not item['description'] and not item['party']:
        warns.append(f'第 {idx + 1} 行没有摘要也没有对方，以后不好找，建议补一个')
    return item, warns, None


def _fmt_item(it):
    amt = it.get('debit') or it.get('credit') or it.get('amount') or 0
    is_in = bool(it.get('debit')) if 'debit' in it else it.get('direction') == 'in'
    way = '收入' if is_in else '支出'
    desc = it.get('description') or it.get('summary') or ''
    return (f"{it.get('date')} {way} {it.get('currency')} {amt:,.2f}"
            f" · {it.get('account_name')}"
            + (f" · {it['party']}" if it.get('party') else '')
            + (f" · {desc}" if desc else ''))


def _ui(p):
    """给前端的最小结构：不含 status/ts 等内部字段。"""
    return {'pid': p['pid'], 'kind': p['kind'], 'title': p['title'],
            'items': p['items'], 'warnings': p['warnings'][:12],
            'danger': p['danger'], 'at': p['at']}


# ---------------------------------------------------------------- 工具：新增
def w_propose_entry(args):
    sid = str(args.get('__sid') or '')
    item, warns, err = _build_item(args)
    if err:
        return {'error': err}, []
    p = add_proposal(sid, 'create', '新增 1 笔流水', [item], warns)
    res = {'ok': True, 'pid': p['pid'], 'count': 1,
           'draft': _fmt_item(item),
           'warnings': warns,
           'note': '草稿已推给用户，等他点「确认写入」才落库。'
                   '回复里用一句话说明你要记什么、有没有疑点即可，不要复述整张表。'}
    res['__ui'] = {'proposal': _ui(p)}
    return res, []


# ---------------------------------------------------------------- 工具：修改
UPD_FIELDS = ('date', 'description', 'party', 'project', 'note',
              'check_no', 'amount', 'direction', 'pending')


def w_propose_update(args):
    sid = str(args.get('__sid') or '')
    eid = _opt_int(args.get('entry_id'))
    if not eid:
        return {'error': '缺少 entry_id。先用 query_entries 找到要改的那一笔'}, []
    row = next(iter(_fetch('SELECT e.*, a.name AS account_name, a.currency '
                           'FROM entries e JOIN accounts a ON a.id=e.account_id '
                           'WHERE e.id=?', (eid,))), None)
    if not row:
        return {'error': f'流水 id={eid} 不存在'}, []

    after, changes, warns = {}, [], []
    for k in ('description', 'party', 'project', 'note', 'check_no'):
        if args.get(k) is not None:
            nv = str(args.get(k) or '').strip()
            if nv != (row[k] or ''):
                changes.append({'field': k, 'label': {'description': '摘要',
                                                      'party': '对方',
                                                      'project': '项目',
                                                      'note': '备注',
                                                      'check_no': '票号'}[k],
                                'from': row[k] or '', 'to': nv})
                after[k] = nv
    if args.get('date') is not None:
        nd = _parse_date(args.get('date'))
        if not nd:
            return {'error': f'日期认不出来：{args.get("date")!r}'}, []
        if nd != row['date']:
            changes.append({'field': 'date', 'label': '日期',
                            'from': row['date'], 'to': nd})
            after['date'] = nd
            warns.append('改动日期会影响账面余额的截止计算')
    if args.get('pending') is not None:
        nv = 1 if args.get('pending') else 0
        if nv != int(row['pending'] or 0):
            changes.append({'field': 'pending', 'label': '状态',
                            'from': '未到账' if row['pending'] else '已入账',
                            'to': '未到账' if nv else '已入账'})
            after['pending'] = nv
    if args.get('amount') is not None:
        amt = abs(_num(args.get('amount')))
        direction = str(args.get('direction') or '').strip().lower()
        old_amt = row['debit'] or row['credit']
        old_dir = 'in' if row['debit'] else 'out'
        direction = direction or old_dir
        nd_debit, nd_credit = (amt, 0.0) if direction in (
            'in', '收入', '收') else (0.0, amt)
        if abs(nd_debit - row['debit']) > 0.005 or abs(nd_credit - row['credit']) > 0.005:
            changes.append({'field': 'amount', 'label': '金额',
                            'from': f"{row['currency']} {old_amt:,.2f}",
                            'to': f"{row['currency']} {amt:,.2f}"})
            after['debit'], after['credit'] = nd_debit, nd_credit
            warns.append(f'金额会被改写：{old_amt:,.2f} → {amt:,.2f}，请核对后在卡片确认')
    if args.get('tags') is not None:
        ids, names = resolve_tags(args.get('tags'))
        changes.append({'field': 'tags', 'label': '标签',
                        'from': '', 'to': '、'.join(names) or '（清空）'})
        after['tag_ids'] = ids
        after['tag_names'] = names
    if not changes:
        return {'error': '没看出要改什么。请给出至少一个要变更的字段'}, []

    item = {'entry_id': eid, 'date': row['date'],
            'account_name': row['account_name'], 'currency': row['currency'],
            'summary': (row['description'] or '')[:40],
            'amount': row['debit'] or row['credit'],
            'direction': 'in' if row['debit'] else 'out',
            'changes': changes, 'after': after, 'before': dict(row)}
    p = add_proposal(sid, 'update', f'修改流水 #{eid}', [item], warns)
    res = {'ok': True, 'pid': p['pid'], 'count': 1,
           'draft': f"#{eid} " + '；'.join(
               f"{c['label']} {c['from']} → {c['to']}" for c in changes)[:200],
           'warnings': warns,
           'note': '草稿已推给用户确认。不要自行断定已改好。'}
    res['__ui'] = {'proposal': _ui(p)}
    return res, []


# ---------------------------------------------------------------- 工具：删除
def w_propose_delete(args):
    sid = str(args.get('__sid') or '')
    raw = args.get('entry_ids') or args.get('ids') or []
    if isinstance(raw, (int, str)):
        raw = [raw]
    ids = [x for x in (_opt_int(v) for v in raw) if x]
    if not ids:
        return {'error': '缺少 entry_ids'}, []
    if len(ids) > MAX_DELETE:
        return {'error': f'一次最多删 {MAX_DELETE} 笔，这次给了 {len(ids)} 笔，请分批'}, []
    items, warns = [], []
    for eid in ids:
        row = next(iter(_fetch(
            'SELECT e.*, a.name AS account_name, a.currency FROM entries e '
            'JOIN accounts a ON a.id=e.account_id WHERE e.id=?', (eid,))), None)
        if not row:
            warns.append(f'#{eid} 不存在，跳过')
            continue
        items.append({'entry_id': eid, 'date': row['date'],
                      'account_name': row['account_name'],
                      'currency': row['currency'],
                      'summary': row['description'] or '',
                      'party': row['party'] or '',
                      'amount': row['debit'] or row['credit'],
                      'direction': 'in' if row['debit'] else 'out'})
    if not items:
        return {'error': '这些 id 都找不到对应流水'}, []
    p = add_proposal(sid, 'delete', f'删除 {len(items)} 笔流水', items, warns,
                     danger=True)
    res = {'ok': True, 'pid': p['pid'], 'count': len(items),
           'draft': '待删除：' + '；'.join(_fmt_item(i) for i in items)[:400],
           'warnings': warns + ['删除必须在卡片上勾选确认后才能执行，执行后会留撤销入口'],
           'note': '删除属高危操作，已标注 danger。回复里说清删哪几笔、为什么，'
                   '提醒用户核对后勾选确认。'}
    res['__ui'] = {'proposal': _ui(p)}
    return res, []


# ---------------------------------------------------------------- 工具：批量
def w_propose_import(args):
    """模型已把对账单整理成数组时走这里。"""
    sid = str(args.get('__sid') or '')
    raw = args.get('entries') or []
    if not isinstance(raw, list) or not raw:
        return {'error': 'entries 为空。传 [{date, amount, direction, description, '
                         'party}] 这样的数组'}, []
    if len(raw) > MAX_ITEMS:
        return {'error': f'一次最多 {MAX_ITEMS} 行，这次 {len(raw)} 行，请分批'}, []
    acc, err = resolve_account(args)
    if err:
        return {'error': err}, []
    items, warns, bad = [], [], []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            bad.append(f'第 {i + 1} 行不是对象')
            continue
        it, w, e = _build_item(row, i, force_account=acc)
        if e:
            bad.append(e)
            continue
        items.append(it)
        warns.extend(w)
    if not items:
        return {'error': '没有一行能用：' + '；'.join(bad[:5])}, []
    p = add_proposal(sid, 'import', f'批量新增 {len(items)} 笔流水', items,
                     bad + warns)
    total_in = sum(i['debit'] for i in items)
    total_out = sum(i['credit'] for i in items)
    res = {'ok': True, 'pid': p['pid'], 'count': len(items), 'bad': bad[:10],
           'total_in': round(total_in, 2), 'total_out': round(total_out, 2),
           'draft': f"{acc['name']}：{len(items)} 笔，收入合计 "
                    f"{total_in:,.2f}、支出合计 {total_out:,.2f}",
           'warnings': (bad + warns)[:12],
           'note': '预检卡片已推给用户。回复里点出总笔数、金额合计、'
                   '几笔疑似重复或格式不对，让用户在卡片上核对后再确认。'}
    res['__ui'] = {'proposal': _ui(p)}
    return res, []


# ---------------------------------------------------------------- 文件解析
def _read_table(path, ext, sheet=None):
    ext = (ext or '').lower()
    if ext in ('csv', 'tsv'):
        raw = open(path, 'rb').read()
        text = ''
        for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'big5', 'latin-1'):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            raise ValueError('文件编码认不出来')
        first = next((ln for ln in text.splitlines() if ln.strip()), '')
        delim = ','
        if ext == 'tsv':
            delim = '\t'
        else:
            best = max([',', ';', '\t', '|'], key=lambda c: first.count(c))
            if first.count(best):
                delim = best
        return [r for r in csv.reader(io.StringIO(text), delimiter=delim)]
    if ext == 'xlsx':
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True, read_only=True)
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if ext == 'xls':
        import xlrd
        bk = xlrd.open_workbook(path)
        sh = (bk.sheet_by_name(sheet) if sheet and sheet in bk.sheet_names()
              else bk.sheet_by_index(0))
        return [sh.row_values(i) for i in range(sh.nrows)]
    raise ValueError('批量导入只支持 Excel(.xlsx/.xls) 与 CSV/TSV')


HINTS = {
    'date': ['交易日期', '记账日期', '日期', 'date', '交易时间', '入账日期', 'value date'],
    'description': ['摘要', '交易摘要', '说明', '用途', '交易说明', 'description',
                    'memo', 'details', 'narrative', 'remark', '备注'],
    'party': ['对方户名', '对方名称', '交易对手', '收款人', '付款人', '对方',
              'party', 'payee', 'payer', 'counterparty'],
    'income': ['收入金额', '存入', '进账', '贷方发生额', '贷方金额', '收入', 'deposit', 'credit'],
    'expense': ['支出金额', '支取', '出账', '借方发生额', '借方金额', '支出', 'withdraw', 'debit'],
    'amount': ['交易金额', '发生额', '金额', 'amount', 'hkd', 'mop', 'cny'],
    'flag': ['借贷标志', '收付标志', '交易方向', '方向', '借贷', 'dc'],
    'balance': ['余额', 'balance'],
}


def _match_cols(header):
    cells = [str(c or '').strip() for c in header]
    low = [c.lower() for c in cells]
    found = {}
    used = set()
    for key, hints in HINTS.items():
        # 先精确、再包含；避免「支出金额」被 amount 抢走
        for h in hints:
            for i, c in enumerate(low):
                if i in used or not c:
                    continue
                if c == h:
                    found[key] = i
                    used.add(i)
                    break
            if key in found:
                break
        if key in found:
            continue
        for h in hints:
            for i, c in enumerate(low):
                if i in used or not c:
                    continue
                if h in c and len(c) <= 14:
                    found[key] = i
                    used.add(i)
                    break
            if key in found:
                break
    return found, cells


def _guess_header(rows):
    """前 12 行里找最像表头的一行（命中的关键词最多）。"""
    best, best_i, best_hit = {}, 0, 0
    for i, r in enumerate(rows[:12]):
        m, _ = _match_cols(r)
        score = len(m)
        if score > best_hit:
            best, best_i, best_hit = m, i, score
    return best, best_i, best_hit


def w_import_file(args):
    """从已上传的 Excel/CSV 直接解析成草稿，模型只负责指认账户和口径。"""
    sid = str(args.get('__sid') or '')
    fid = str(args.get('fid') or args.get('file_id') or '').strip()
    if not fid:
        return {'error': '缺少 fid（上传接口返回的文件 id）'}, []
    from agent_files import load_att
    att = load_att(fid)
    if not att:
        return {'error': f'找不到文件 {fid}'}, []
    ext = att.get('ext') or ''
    if ext not in ('xlsx', 'xls', 'csv', 'tsv'):
        return {'error': f'这个文件是 .{ext}，只能导入 Excel/CSV 对账单'}, []
    acc, err = resolve_account(args)
    if err:
        return {'error': err}, []
    path = att.get('path')
    try:
        rows_in = _read_table(path, ext, args.get('sheet'))
    except Exception as exc:
        return {'error': f'读文件失败：{exc}'}, []
    if not rows_in:
        return {'error': '文件里没有内容'}, []

    header, hrow, hit = _guess_header(rows_in)
    cols = args.get('cols') if isinstance(args.get('cols'), dict) else {}
    # 允许模型用列名覆盖自动识别
    if cols:
        head_cells = [str(c or '').strip() for c in rows_in[hrow]]
        for k, v in cols.items():
            if isinstance(v, int):
                header[k] = v
            else:
                for i, c in enumerate(head_cells):
                    if c == str(v).strip():
                        header[k] = i
                        break
    if 'date' not in header:
        return {'error': '没认出日期列。请告诉我哪一列是日期（cols.date=列名），'
                         f'表头是：{ [str(c or "") for c in rows_in[hrow]][:12] }'}, []

    has_pair = 'income' in header or 'expense' in header
    if not has_pair and 'amount' not in header:
        return {'error': '没认出金额列（既没有收入/支出分列，也没有单独的金额列）。'
                         f'表头是：{ [str(c or "") for c in rows_in[hrow]][:12] }'}, []

    default_year = args.get('year') or datetime.now().year
    sign = str(args.get('sign') or 'auto').lower()
    items, warns, bad = [], [], []
    for ri, r in enumerate(rows_in[hrow + 1:], start=hrow + 2):
        if not any(str(c or '').strip() for c in r):
            continue
        def cell(k):
            i = header.get(k)
            if i is None or i >= len(r):
                return ''
            return r[i]

        d = _parse_date(cell('date'), default_year)
        if not d:
            first = str(cell('date') or '').strip()
            # 小计/合计/总计这类汇总行直接跳过，不算错误
            if re.match(r'^(合计|小计|总计|总额|合\s*计|total|subtotal)', first, re.I):
                continue
            if any(str(c or '').strip() for c in r):
                bad.append(f'第 {ri} 行日期没认出：{cell("date")!r}')
            continue
        debit = credit = 0.0
        if has_pair:
            debit = abs(_num(cell('income')))
            credit = abs(_num(cell('expense')))
            flag = str(cell('flag') or '').strip()
            if debit and credit:
                warns.append(f'第 {ri} 行收入支出同时有值，只取金额大的那个')
                if debit >= credit:
                    credit = 0.0
                else:
                    debit = 0.0
            if not (debit or credit) and 'amount' in header:
                raw_amt = _num(cell('amount'))
                if raw_amt:
                    if '贷' in flag or '收' in flag or 'in' in flag.lower():
                        debit = abs(raw_amt)
                    else:
                        credit = abs(raw_amt)
        else:
            raw_amt = _num(cell('amount'))
            flag = str(cell('flag') or '').strip().lower()
            if raw_amt == 0:
                bad.append(f'第 {ri} 行金额为 0')
                continue
            if sign == 'in':
                debit, credit = abs(raw_amt), 0.0
            elif sign == 'out':
                debit, credit = 0.0, abs(raw_amt)
            elif raw_amt < 0:
                debit, credit = 0.0, abs(raw_amt)
            elif any(x in flag for x in ('支', '付', '出', 'd', 'c')):
                debit, credit = 0.0, abs(raw_amt)
            elif any(x in flag for x in ('收', '入', '进')):
                debit, credit = abs(raw_amt), 0.0
            else:
                debit, credit = abs(raw_amt), 0.0
                warns.append(f'第 {ri} 行看不出收支方向，先按收入处理')
        if not (debit or credit):
            continue
        it, w, e = _build_item({
            'date': d, 'debit': debit, 'credit': credit,
            'description': str(cell('description') or '').strip()[:120],
            'party': str(cell('party') or '').strip()[:80],
            'project': str(cell('project') or '').strip()[:60],
            'note': str(cell('note') or '').strip()[:200],
        }, len(items), force_account=acc)
        if e:
            bad.append(e)
            continue
        items.append(it)
        warns.extend(w)
        if len(items) >= MAX_ITEMS:
            warns.append(f'文件行数超过 {MAX_ITEMS}，只取了前 {MAX_ITEMS} 行，'
                         f'剩下的请分批导入')
            break

    if not items:
        return {'error': '一行都没解析出来：' + '；'.join(bad[:5])}, []

    p = add_proposal(sid, 'import', f'导入 {len(items)} 笔流水', items,
                     (bad + warns)[:40])
    total_in = sum(i['debit'] for i in items)
    total_out = sum(i['credit'] for i in items)
    res = {'ok': True, 'pid': p['pid'], 'count': len(items),
           'file': att.get('name'), 'account': acc['name'],
           'columns': {k: v for k, v in header.items() if k != 'balance'},
           'bad': bad[:10],
           'total_in': round(total_in, 2), 'total_out': round(total_out, 2),
           'draft': (f"{att.get('name')} → {acc['name']}：{len(items)} 笔，"
                     f"收入 {total_in:,.2f}、支出 {total_out:,.2f}"),
           'warnings': (bad + warns)[:12],
           'note': '预检卡片已推给用户。告诉他识别到几笔、合计多少、'
                   '多少行有问题，请他在卡片里核对后确认。'}
    res['__ui'] = {'proposal': _ui(p)}
    return res, []


# ---------------------------------------------------------------- 执行
def _ensure_tags(db, names):
    ids = []
    for raw in names or []:
        n = str(raw or '').strip().lstrip('#')
        if not n:
            continue
        r = db.execute('SELECT id FROM tags WHERE name=?', (n,)).fetchone()
        if r:
            ids.append(r['id'])
        else:
            cur = db.execute('INSERT INTO tags(name,color) VALUES(?,?)',
                             (n, '#3b82f6'))
            ids.append(cur.lastrowid)
    return ids


def _entry_payload(it):
    return {'account_id': it['account_id'], 'date': it['date'],
            'check_no': it.get('check_no') or '',
            'description': it.get('description') or '',
            'party': it.get('party') or '', 'project': it.get('project') or '',
            'note': it.get('note') or '', 'debit': it.get('debit') or 0,
            'credit': it.get('credit') or 0,
            'pending': 1 if it.get('pending') else 0}


def _apply_edit(it, ed):
    """把前端卡片上的改动落到草稿项上。"""
    out = dict(it)
    for k in ('description', 'party', 'project', 'note'):
        if k in ed:
            out[k] = str(ed.get(k) or '').strip()
    if 'date' in ed:
        d = _parse_date(ed.get('date'))
        if d:
            out['date'] = d
    if 'amount' in ed or 'direction' in ed:
        amt = abs(_num(ed.get('amount', out.get('debit') or out.get('credit'))))
        direction = str(ed.get('direction') or
                        ('in' if out.get('debit') else 'out')).lower()
        if direction in ('in', '收入', '收'):
            out['debit'], out['credit'] = amt, 0.0
        else:
            out['debit'], out['credit'] = 0.0, amt
    if 'pending' in ed:
        out['pending'] = 1 if ed.get('pending') else 0
    return out


def _log(rec):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(WRITE_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass


def apply_proposal(pid, edits=None, ack=False):
    """真正落库。返回 (payload, http_status)。"""
    p = get_proposal(pid)
    if not p:
        return {'error': '这张草稿已过期或不存在，请让 AI 重新生成'}, 404
    if p['status'] != 'pending':
        return {'error': f"这张草稿已经{'执行过' if p['status'] == 'done' else '取消了'}"}, 409
    if p['danger'] and not ack:
        return {'error': '这是删除操作，需要先勾选「我已核对，确认删除」'}, 400

    A = _wdb()
    db = A.get_db()
    edits = edits or {}
    wid = 'w' + uuid.uuid4().hex[:10]
    done, fails = [], []
    undo = {'type': p['kind'], 'entries': [], 'tags': []}

    with A._write_lock:
        if p['kind'] in ('create', 'import'):
            for i, it0 in enumerate(p['items']):
                it = _apply_edit(it0, edits.get(str(i)) or edits.get(i) or {})
                err, f = A.validate_entry_payload(_entry_payload(it))
                if err:
                    fails.append(f'第 {i + 1} 行：{err}')
                    continue
                cur = db.execute(
                    'INSERT INTO entries(account_id,date,check_no,description,'
                    'party,debit,credit,pending,project,note,created_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (f['account_id'], f['date'], f['check_no'], f['description'],
                     f['party'], f['debit'], f['credit'], f['pending'],
                     f['project'], f['note'], A.now_iso()))
                eid = cur.lastrowid
                tids = list(it.get('tag_ids') or []) + _ensure_tags(
                    db, it.get('tag_names'))
                if tids:
                    A.set_entry_tags(db, eid, tids)
                undo['entries'].append({'id': eid})
                done.append({'id': eid, 'date': f['date'],
                             'amount': f['debit'] or f['credit'],
                             'direction': 'in' if f['debit'] else 'out',
                             'account_id': f['account_id']})
        elif p['kind'] == 'update':
            it = p['items'][0]
            ed = edits.get('0') or edits.get(0) or {}
            after = dict(it.get('after') or {})
            for k, v in ed.items():
                if k in ('description', 'party', 'project', 'note', 'check_no'):
                    after[k] = str(v or '').strip()
                elif k == 'date':
                    nd = _parse_date(v)
                    if nd:
                        after['date'] = nd
            row = db.execute('SELECT * FROM entries WHERE id=?',
                             (it['entry_id'],)).fetchone()
            if not row:
                return {'error': f"流水 #{it['entry_id']} 已不存在"}, 404
            merged = dict(row)
            merged.update({k: v for k, v in after.items()
                           if k in ('date', 'debit', 'credit', 'description',
                                    'party', 'project', 'note', 'check_no',
                                    'pending')})
            err, f = A.validate_entry_payload(_entry_payload({
                'account_id': row['account_id'], 'date': merged['date'],
                'check_no': merged['check_no'], 'description': merged['description'],
                'party': merged['party'], 'project': merged['project'],
                'note': merged['note'], 'debit': merged['debit'],
                'credit': merged['credit'], 'pending': merged['pending']}))
            if err:
                return {'error': err}, 400
            old_tags = [r['id'] for r in db.execute(
                'SELECT tag_id AS id FROM entry_tags WHERE entry_id=?',
                (it['entry_id'],))]
            db.execute('UPDATE entries SET date=?, check_no=?, description=?, '
                       'party=?, debit=?, credit=?, pending=?, project=?, note=? '
                       'WHERE id=?',
                       (f['date'], f['check_no'], f['description'], f['party'],
                        f['debit'], f['credit'], f['pending'], f['project'],
                        f['note'], it['entry_id']))
            if 'tag_ids' in after or 'tag_names' in after:
                tids = list(after.get('tag_ids') or []) + _ensure_tags(
                    db, after.get('tag_names'))
                A.set_entry_tags(db, it['entry_id'], tids)
            undo['entries'].append({
                'id': it['entry_id'], 'before': dict(row), 'tags': old_tags})
            done.append({'id': it['entry_id'], 'date': f['date'],
                         'amount': f['debit'] or f['credit'],
                         'direction': 'in' if f['debit'] else 'out',
                         'account_id': row['account_id']})
        elif p['kind'] == 'delete':
            for it in p['items']:
                eid = it['entry_id']
                row = db.execute('SELECT * FROM entries WHERE id=?', (eid,)).fetchone()
                if not row:
                    fails.append(f'#{eid} 已不存在')
                    continue
                tags = [r['id'] for r in db.execute(
                    'SELECT tag_id AS id FROM entry_tags WHERE entry_id=?', (eid,))]
                db.execute('DELETE FROM entry_tags WHERE entry_id=?', (eid,))
                db.execute('DELETE FROM entries WHERE id=?', (eid,))
                # 附件文件保留在磁盘上，撤销时还能挂回去
                undo['entries'].append({'row': dict(row), 'tags': tags})
                done.append({'id': eid, 'date': row['date'],
                             'amount': row['debit'] or row['credit'],
                             'direction': 'in' if row['debit'] else 'out'})
        db.commit()

    p['status'] = 'done' if done else 'failed'
    p['wid'] = wid if done else None
    p['result'] = {'done': len(done), 'fails': fails[:10]}
    with _lock:
        _save()
    if done:
        _log({'wid': wid, 'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
              'pid': pid, 'kind': p['kind'], 'sid': p.get('sid'),
              'count': len(done), 'items': done, 'undo': undo})
    return {'ok': bool(done), 'done': len(done), 'failed': len(fails),
            'fails': fails[:10], 'wid': wid if done else None,
            'items': done, 'kind': p['kind'],
            'message': (f"已写入 {len(done)} 笔" if done else '没有写入任何记录')
                       + (f"，{len(fails)} 笔失败" if fails else '')}, 200


def undo_write(wid):
    rec = None
    try:
        with open(WRITE_LOG, encoding='utf-8') as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get('wid') == wid:
                    rec = r
    except OSError:
        pass
    if not rec:
        return {'error': '找不到这条写入记录，可能已经撤销或日志被清理'}, 404
    if rec.get('undone'):
        return {'error': '这次写入已经撤销过了'}, 409

    A = _wdb()
    db = A.get_db()
    undo = rec.get('undo') or {}
    n = 0
    with A._write_lock:
        if undo.get('type') in ('create', 'import'):
            for e in undo.get('entries') or []:
                db.execute('DELETE FROM entry_tags WHERE entry_id=?', (e['id'],))
                db.execute('DELETE FROM entries WHERE id=?', (e['id'],))
                n += 1
        elif undo.get('type') == 'update':
            for e in undo.get('entries') or []:
                b = e.get('before') or {}
                db.execute('UPDATE entries SET date=?, check_no=?, description=?, '
                           'party=?, debit=?, credit=?, pending=?, project=?, '
                           'note=? WHERE id=?',
                           (b.get('date'), b.get('check_no'), b.get('description'),
                            b.get('party'), b.get('debit'), b.get('credit'),
                            b.get('pending'), b.get('project'), b.get('note'),
                            e['id']))
                A.set_entry_tags(db, e['id'], e.get('tags') or [])
                n += 1
        elif undo.get('type') == 'delete':
            for e in undo.get('entries') or []:
                r = e.get('row') or {}
                if not r:
                    continue
                db.execute(
                    'INSERT OR REPLACE INTO entries(id,account_id,date,check_no,'
                    'description,party,debit,credit,pending,project,note,created_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (r.get('id'), r.get('account_id'), r.get('date'),
                     r.get('check_no'), r.get('description'), r.get('party'),
                     r.get('debit'), r.get('credit'), r.get('pending'),
                     r.get('project'), r.get('note'), r.get('created_at')))
                A.set_entry_tags(db, r.get('id'), e.get('tags') or [])
                n += 1
        db.commit()

    rec['undone'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new_lines = []
    try:
        with open(WRITE_LOG, encoding='utf-8') as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    new_lines.append(line)
                    continue
                new_lines.append(json.dumps(r if r.get('wid') != wid else rec,
                                            ensure_ascii=False) + '\n')
        with open(WRITE_LOG, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
    except OSError:
        pass
    return {'ok': True, 'undone': n, 'message': f'已撤销 {n} 笔'}, 200


# ---------------------------------------------------------------- HTTP 端点
@bp.post('/api/agent/proposal/<pid>/confirm')
def api_confirm(pid):
    data = request.get_json(silent=True) or {}
    payload, code = apply_proposal(pid, data.get('edits') or {},
                                   bool(data.get('ack')))
    return jsonify(payload), code


@bp.post('/api/agent/proposal/<pid>/cancel')
def api_cancel(pid):
    p = get_proposal(pid)
    if not p:
        return jsonify({'error': '草稿不存在或已过期'}), 404
    if p['status'] == 'pending':
        p['status'] = 'cancelled'
        with _lock:
            _save()
    return jsonify({'ok': True})


@bp.get('/api/agent/proposal/<pid>')
def api_get(pid):
    p = get_proposal(pid)
    if not p:
        return jsonify({'error': '草稿不存在或已过期'}), 404
    return jsonify({'proposal': _ui(p), 'status': p['status'],
                    'result': p.get('result'), 'wid': p.get('wid')})


@bp.post('/api/agent/undo/<wid>')
def api_undo(wid):
    payload, code = undo_write(wid)
    return jsonify(payload), code


# ---------------------------------------------------------------- 工具声明
DEFS = [
    {'type': 'function', 'function': {
        'name': 'propose_entry',
        'description': '记一笔流水（生成待确认草稿，不会立刻写库）。用户说「记一笔」'
                       '「帮我加一条」「这笔支出补记一下」时用。金额一律传正数，'
                       '用 direction 区分收支。草稿会以卡片推给用户，他点确认才真正写入。',
        'parameters': {'type': 'object', 'properties': {
            'account_id': {'type': 'integer', 'description': '账户 id，优先用（从 list_accounts 拿）'},
            'account': {'type': 'string', 'description': '账户名或账号，没有 id 时用'},
            'company': {'type': 'string', 'description': '公司名，多公司同名账户时用来定位'},
            'date': {'type': 'string', 'description': 'YYYY-MM-DD'},
            'amount': {'type': 'number', 'description': '金额，正数'},
            'direction': {'type': 'string', 'enum': ['in', 'out'],
                          'description': 'in=收入/进账，out=支出/付款'},
            'description': {'type': 'string', 'description': '摘要'},
            'party': {'type': 'string', 'description': '对方单位/个人'},
            'project': {'type': 'string', 'description': '项目/工程'},
            'note': {'type': 'string', 'description': '备注'},
            'tags': {'type': 'array', 'items': {'type': 'string'}, 'description': '标签名'},
            'pending': {'type': 'boolean', 'description': '是否未兑现/未到账'},
        }, 'required': ['date', 'amount', 'direction']}}},
    {'type': 'function', 'function': {
        'name': 'propose_update_entry',
        'description': '修改一笔已有流水的草稿（不会立刻写库）。只传要改的字段。'
                       '改金额时同时给 amount 与 direction。先用 query_entries 找到 entry_id。',
        'parameters': {'type': 'object', 'properties': {
            'entry_id': {'type': 'integer', 'description': '要改的流水 id'},
            'date': {'type': 'string'},
            'description': {'type': 'string'},
            'party': {'type': 'string'},
            'project': {'type': 'string'},
            'note': {'type': 'string'},
            'check_no': {'type': 'string'},
            'amount': {'type': 'number', 'description': '新的金额（正数），会改写借贷方'},
            'direction': {'type': 'string', 'enum': ['in', 'out']},
            'pending': {'type': 'boolean'},
            'tags': {'type': 'array', 'items': {'type': 'string'},
                     'description': '完整替换标签；传空数组表示清空'},
        }, 'required': ['entry_id']}}},
    {'type': 'function', 'function': {
        'name': 'propose_delete_entries',
        'description': '删除流水的草稿（高危，用户必须在卡片上勾选确认才会执行）。'
                       '用户明确说「删掉」「这笔重复了去掉」时才用；只是怀疑重复就先问一句。',
        'parameters': {'type': 'object', 'properties': {
            'entry_ids': {'type': 'array', 'items': {'type': 'integer'},
                          'description': '要删的流水 id 列表，一次最多 20 笔'},
            'reason': {'type': 'string', 'description': '为什么要删，会显示在卡片上'},
        }, 'required': ['entry_ids']}}},
    {'type': 'function', 'function': {
        'name': 'import_file',
        'description': '把用户上传的银行对账单（Excel/CSV）解析成待确认的流水草稿。'
                       '比逐条 propose_entry 准确得多，优先用它。自动识别日期/摘要/对方/'
                       '收入/支出列；识别不对时再用 cols 指定列名。只解析不写库。',
        'parameters': {'type': 'object', 'properties': {
            'fid': {'type': 'string', 'description': '上传接口返回的文件 id'},
            'account_id': {'type': 'integer', 'description': '导入到哪个账户'},
            'account': {'type': 'string', 'description': '账户名，没有 id 时用'},
            'company': {'type': 'string'},
            'sheet': {'type': 'string', 'description': '工作表名（Excel 多表时指定）'},
            'sign': {'type': 'string', 'enum': ['auto', 'in', 'out'],
                     'description': '单金额列且无方向标志时，统一按收入还是支出解释'},
            'year': {'type': 'integer', 'description': '日期缺年份时补哪一年'},
            'cols': {'type': 'object', 'description':
                     '列名映射，如 {"date":"交易日期","amount":"交易金额"}；'
                     '键可用 date/description/party/income/expense/amount/flag'},
        }, 'required': ['fid']}}},
    {'type': 'function', 'function': {
        'name': 'propose_import',
        'description': '批量新增流水的草稿（自己整理好数组时用）。上传的 Excel/CSV '
                       '优先用 import_file，只有手打的行或修复个别行时才用它。',
        'parameters': {'type': 'object', 'properties': {
            'account_id': {'type': 'integer'},
            'account': {'type': 'string'},
            'company': {'type': 'string'},
            'entries': {'type': 'array', 'items': {
                'type': 'object', 'properties': {
                    'date': {'type': 'string'},
                    'amount': {'type': 'number'},
                    'direction': {'type': 'string', 'enum': ['in', 'out']},
                    'description': {'type': 'string'},
                    'party': {'type': 'string'},
                    'project': {'type': 'string'},
                    'note': {'type': 'string'},
                }, 'required': ['date', 'amount']}},
        }, 'required': ['entries']}}},
]

WRITE_TOOLS = DEFS
WRITE_IMPL = {
    'propose_entry': w_propose_entry,
    'propose_update_entry': w_propose_update,
    'propose_delete_entries': w_propose_delete,
    'import_file': w_import_file,
    'propose_import': w_propose_import,
}
WRITE_LABELS = {
    'propose_entry': '起草一笔流水',
    'propose_update_entry': '起草修改',
    'propose_delete_entries': '起草删除',
    'import_file': '解析对账单',
    'propose_import': '起草批量导入',
}
