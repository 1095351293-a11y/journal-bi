# -*- coding: utf-8 -*-
"""Agent 管理类工具 —— 把 BI 系统「页面能点的一切」变成 AI 能调的动作

覆盖范围
--------
* 公司：新建 / 改名 / 删除（级联删账户与流水，需二次确认）
* 账户：新建 / 修改 / 删除（级联删流水，需二次确认）
* 标签：新建 / 改名改色 / 删除
* 定期存款：新建 / 修改 / 删除
* 汇率：批量设置
* 库存现金盘点单：新建 / 修改 / 删除
* 系统：数据概况、备份

权限
----
日常维护（新建/修改）走 ``admin`` 策略，默认直接执行；
删除类走 ``admin_danger``，默认必须 ``confirm=true`` 才执行，模型要先向用户
说明影响、得到明确同意后才能带这个参数重调。
每次执行都追加一行 ``data/agent_admin_log.jsonl``。
"""
import json
import os
import zipfile
from datetime import date, datetime

from agent_write import _fetch, _num, _opt_int, _parse_date, _wdb

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'journal.db')
ADMIN_LOG = os.path.join(DATA_DIR, 'agent_admin_log.jsonl')
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')


# ---------------------------------------------------------------- 公共
def _names(sql, params=()):
    return _fetch(sql, params)


def _companies():
    return _fetch('SELECT id, name FROM companies ORDER BY sort, id')


def _company(v):
    """按 id 或名称定位公司，返回 (row, err)。"""
    v = str(v or '').strip()
    if not v:
        return None, '缺少公司（传 company_id 或 company 名称）'
    cid = _opt_int(v)
    rows = _companies()
    if cid:
        for c in rows:
            if c['id'] == cid:
                return c, None
        return None, f'公司 id={cid} 不存在'
    for c in rows:
        if v == c['name']:
            return c, None
    hits = [c for c in rows if v in (c['name'] or '') or (c['name'] or '') in v]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, ('有多家公司对得上，请说清楚：'
                      + '、'.join(f"{c['name']}(id={c['id']})" for c in hits[:8]))
    if not rows:
        return None, '系统里还没有任何公司，先建一家（manage_company action=create）'
    return None, ('找不到公司「' + v + '」，现有：'
                  + '、'.join(f"{c['name']}(id={c['id']})" for c in rows[:10]))


def _audit(tool, action, args, result, sid=''):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        rec = {'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'tool': tool, 'action': action, 'sid': sid,
               'args': {k: v for k, v in (args or {}).items()
                        if not str(k).startswith('__')},
               'ok': not (isinstance(result, dict) and result.get('error')),
               'result': (str(result.get('error'))[:300]
                          if isinstance(result, dict) and result.get('error')
                          else str(result.get('message') or result.get('note') or '')[:300])}
        with open(ADMIN_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass


def _need_confirm(what, impact):
    return {'error': f'{what}属高危操作，需要用户明确同意后才能执行',
            'need_confirm': True,
            'impact': impact,
            'note': ('把上面这些影响原样告诉用户。他已经明确说「确认/删吧/我同意」时，'
                     '立刻带 confirm=true 重新调用一次把事办完；'
                     '他还没表态时，就停在这里等。')}


def _db():
    return _wdb()


# ---------------------------------------------------------------- 系统概况
def t_system_info(args):
    from agent_perm import describe, load as perm_load
    accs = _fetch('SELECT COUNT(*) n FROM accounts')[0]['n']
    ents = _fetch('SELECT COUNT(*) n, MIN(date) a, MAX(date) b FROM entries')[0]
    tags = _fetch('SELECT COUNT(*) n FROM tags')[0]['n']
    deps = _fetch('SELECT COUNT(*) n FROM deposits')[0]['n']
    cc = _fetch('SELECT COUNT(*) n FROM cash_counts')[0]['n']
    try:
        size = round(os.path.getsize(DB_PATH) / 1024 / 1024, 2)
    except OSError:
        size = 0
    backups = []
    if os.path.isdir(BACKUP_DIR):
        for fn in sorted(os.listdir(BACKUP_DIR), reverse=True)[:5]:
            p = os.path.join(BACKUP_DIR, fn)
            try:
                backups.append({'file': fn,
                                'mb': round(os.path.getsize(p) / 1024 / 1024, 2)})
            except OSError:
                pass
    return {'companies': _companies(), 'accounts': accs,
            'entries': {'count': ents['n'], 'from': ents['a'], 'to': ents['b']},
            'tags': tags, 'deposits': deps, 'cash_counts': cc,
            'db_mb': size, 'recent_backups': backups,
            'permissions': perm_load(), 'permission_summary': describe(),
            'data_dir': DATA_DIR,
            'note': '数据库文件与备份都在 data_dir 下；恢复备份要在页面「设置」里操作，'
                    '没有开放给 AI。'}, []


def t_backup_now(args):
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        path = os.path.join(BACKUP_DIR, f'journal_{stamp}.zip')
        updir = os.path.join(DATA_DIR, 'uploads')
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
            if os.path.exists(DB_PATH):
                z.write(DB_PATH, 'journal.db')
            if os.path.isdir(updir):
                for fn in os.listdir(updir):
                    z.write(os.path.join(updir, fn), f'uploads/{fn}')
        # 只留最近 10 个自动备份，别把磁盘占满
        olds = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith('journal_'))
        for fn in olds[:-10]:
            try:
                os.remove(os.path.join(BACKUP_DIR, fn))
            except OSError:
                pass
        mb = round(os.path.getsize(path) / 1024 / 1024, 2)
        return {'ok': True, 'file': path, 'mb': mb,
                'message': f'已备份到 {path}（{mb} MB）'}, []
    except OSError as exc:
        return {'error': f'备份失败：{exc}'}, []


# ---------------------------------------------------------------- 公司
def t_manage_company(args):
    action = str(args.get('action') or '').strip().lower()
    sid = str(args.get('__sid') or '')
    A = _db()
    db = A.get_db()

    if action in ('create', 'add', '新建'):
        name = str(args.get('name') or '').strip()
        if not name:
            return {'error': '公司名称不能为空'}, []
        if any((c['name'] or '').strip() == name for c in _companies()):
            return {'error': f'已经有一家公司叫「{name}」了'}, []
        with A._write_lock:
            cur = db.execute('INSERT INTO companies(name) VALUES(?)', (name,))
            db.commit()
        res = {'ok': True, 'id': cur.lastrowid, 'name': name,
               'message': f'已新建公司「{name}」(id={cur.lastrowid})'}
        _audit('manage_company', 'create', args, res, sid)
        return res, []

    if action in ('rename', 'update', '改名', '修改'):
        c, err = _company(args.get('company_id') or args.get('company') or args.get('id'))
        if err:
            return {'error': err}, []
        name = str(args.get('name') or args.get('new_name') or '').strip()
        if not name:
            return {'error': '新名称不能为空'}, []
        with A._write_lock:
            db.execute('UPDATE companies SET name=? WHERE id=?', (name, c['id']))
            db.commit()
        res = {'ok': True, 'id': c['id'], 'from': c['name'], 'to': name,
               'message': f"公司「{c['name']}」已改名为「{name}」"}
        _audit('manage_company', 'rename', args, res, sid)
        return res, []

    if action in ('delete', 'remove', '删除'):
        c, err = _company(args.get('company_id') or args.get('company') or args.get('id'))
        if err:
            return {'error': err}, []
        accs = _fetch('SELECT id, name FROM accounts WHERE company_id=?', (c['id'],))
        n_ent = 0
        if accs:
            ph = ','.join('?' * len(accs))
            n_ent = _fetch(f'SELECT COUNT(*) n FROM entries WHERE account_id IN ({ph})',
                           [a['id'] for a in accs])[0]['n']
        impact = (f"公司「{c['name']}」下 {len(accs)} 个账户、{n_ent} 笔流水、"
                  f"以及其定期存款和现金盘点单将一并删除，且不可恢复（除非有备份）")
        if not args.get('confirm'):
            return _need_confirm('删除公司', impact), []
        with A._write_lock:
            for a in accs:
                A._delete_account_db(db, a['id'])
            db.execute('DELETE FROM deposits WHERE company_id=?', (c['id'],))
            db.execute('DELETE FROM cash_count_denoms WHERE item_id IN '
                       '(SELECT id FROM cash_count_items WHERE count_id IN '
                       '(SELECT id FROM cash_counts WHERE company_id=?))', (c['id'],))
            db.execute('DELETE FROM cash_count_items WHERE count_id IN '
                       '(SELECT id FROM cash_counts WHERE company_id=?)', (c['id'],))
            db.execute('DELETE FROM cash_counts WHERE company_id=?', (c['id'],))
            db.execute('DELETE FROM companies WHERE id=?', (c['id'],))
            db.commit()
        res = {'ok': True, 'deleted': {'accounts': len(accs), 'entries': n_ent},
               'message': f"已删除公司「{c['name']}」及其 {len(accs)} 个账户、{n_ent} 笔流水"}
        _audit('manage_company', 'delete', args, res, sid)
        return res, []

    return {'error': 'action 只能是 create / rename / delete'}, []


# ---------------------------------------------------------------- 账户
def t_manage_account(args):
    action = str(args.get('action') or '').strip().lower()
    sid = str(args.get('__sid') or '')
    A = _db()
    db = A.get_db()

    if action in ('create', 'add', '新建'):
        from agent_write import resolve_account
        name = str(args.get('name') or args.get('account') or '').strip()
        if not name:
            return {'error': '账户名称不能为空'}, []
        cid = _opt_int(args.get('company_id'))
        if not cid and args.get('company'):
            c, err = _company(args.get('company'))
            if err:
                return {'error': err}, []
            cid = c['id']
        if not cid:
            rows = _companies()
            if len(rows) == 1:
                cid = rows[0]['id']
            else:
                return {'error': '要指定公司：' + '、'.join(
                    f"{c['name']}(id={c['id']})" for c in rows[:8])}, []
        if not db.execute('SELECT 1 FROM companies WHERE id=?', (cid,)).fetchone():
            return {'error': f'公司 id={cid} 不存在'}, []
        kind = 'cash' if str(args.get('kind') or 'bank') == 'cash' else 'bank'
        cur = db.execute(
            'INSERT INTO accounts(company_id,name,currency,bank,account_no,opening,'
            'kind,opening_date) VALUES(?,?,?,?,?,?,?,?)',
            (cid, name, str(args.get('currency') or 'MOP').upper(),
             str(args.get('bank') or ''), str(args.get('account_no') or ''),
             _num(args.get('opening')), kind,
             _parse_date(args.get('opening_date')) or ''))
        db.commit()
        res = {'ok': True, 'id': cur.lastrowid,
               'message': f'已新建账户「{name}」(id={cur.lastrowid})，'
                          f"币种 {str(args.get('currency') or 'MOP').upper()}"}
        _audit('manage_account', 'create', args, res, sid)
        return res, []

    if action in ('update', 'rename', '修改'):
        from agent_write import resolve_account
        acc, err = resolve_account({**args, 'account_id': args.get('account_id') or args.get('id')})
        if err:
            return {'error': err}, []
        warns = []
        new_opening = args.get('opening')
        if new_opening not in (None, '') and abs(_num(new_opening) - (acc.get('opening') or 0)) > 0.005:
            warns.append(f"期初余额会从 {acc.get('opening') or 0:,.2f} 改为 "
                         f"{_num(new_opening):,.2f}，所有余额都会跟着变")
        kind = str(args.get('kind') or acc.get('kind') or 'bank')
        if kind not in ('bank', 'cash'):
            kind = 'bank'
        db.execute(
            'UPDATE accounts SET name=?, currency=?, bank=?, account_no=?, opening=?, '
            'kind=?, opening_date=? WHERE id=?',
            (str(args.get('name') or acc['name']).strip() or acc['name'],
             str(args.get('currency') or acc['currency']).upper(),
             str(args.get('bank') if args.get('bank') is not None else acc.get('bank') or ''),
             str(args.get('account_no') if args.get('account_no') is not None
                 else acc.get('account_no') or ''),
             _num(new_opening) if new_opening not in (None, '') else acc.get('opening') or 0,
             kind,
             (_parse_date(args.get('opening_date'))
              if args.get('opening_date') is not None
              else acc.get('opening_date') or ''),
             acc['id']))
        db.commit()
        res = {'ok': True, 'id': acc['id'], 'warnings': warns,
               'message': f"账户「{acc['name']}」已更新"}
        _audit('manage_account', 'update', args, res, sid)
        return res, []

    if action in ('delete', 'remove', '删除'):
        from agent_write import resolve_account
        acc, err = resolve_account({**args, 'account_id': args.get('account_id') or args.get('id')})
        if err:
            return {'error': err}, []
        n = _fetch('SELECT COUNT(*) n FROM entries WHERE account_id=?', (acc['id'],))[0]['n']
        impact = (f"账户「{acc['name']}」及其 {n} 笔流水、相关附件和标签关联将全部删除，"
                  f'不可恢复（除非有备份）')
        if not args.get('confirm'):
            return _need_confirm('删除账户', impact), []
        with A._write_lock:
            A._delete_account_db(db, acc['id'])
            db.commit()
        res = {'ok': True, 'deleted': {'entries': n},
               'message': f"已删除账户「{acc['name']}」及 {n} 笔流水"}
        _audit('manage_account', 'delete', args, res, sid)
        return res, []

    return {'error': 'action 只能是 create / update / delete'}, []


# ---------------------------------------------------------------- 标签
def t_manage_tag(args):
    action = str(args.get('action') or '').strip().lower()
    sid = str(args.get('__sid') or '')
    A = _db()
    db = A.get_db()

    def _tag(v):
        v = str(v or '').strip().lstrip('#')
        if not v:
            return None, '缺少标签名或 id'
        tid = _opt_int(v)
        rows = _fetch('SELECT id, name, color FROM tags ORDER BY id')
        if tid:
            for t in rows:
                if t['id'] == tid:
                    return t, None
        for t in rows:
            if t['name'] == v:
                return t, None
        return None, ('没有这个标签，现有：' + '、'.join(t['name'] for t in rows[:15])
                      if rows else '还没有任何标签')

    if action in ('create', 'add', '新建'):
        name = str(args.get('name') or args.get('tag') or '').strip().lstrip('#')
        if not name:
            return {'error': '标签名不能为空'}, []
        old = _fetch('SELECT id FROM tags WHERE name=?', (name,))
        if old:
            return {'ok': True, 'id': old[0]['id'], 'existed': True,
                    'message': f'标签「{name}」已存在，没重复建'}, []
        cur = db.execute('INSERT INTO tags(name, color) VALUES(?,?)',
                         (name, str(args.get('color') or '#3b82f6')))
        db.commit()
        res = {'ok': True, 'id': cur.lastrowid, 'message': f'已新建标签「{name}」'}
        _audit('manage_tag', 'create', args, res, sid)
        return res, []

    if action in ('update', 'rename', '改名', '修改'):
        t, err = _tag(args.get('tag_id') or args.get('id') or args.get('tag') or args.get('name'))
        if err:
            return {'error': err}, []
        name = str(args.get('new_name') or args.get('name') or t['name']).strip().lstrip('#')
        color = str(args.get('color') or t['color'])
        db.execute('UPDATE tags SET name=?, color=? WHERE id=?', (name, color, t['id']))
        db.commit()
        res = {'ok': True, 'id': t['id'], 'message': f"标签「{t['name']}」已改为「{name}」"}
        _audit('manage_tag', 'update', args, res, sid)
        return res, []

    if action in ('delete', 'remove', '删除'):
        t, err = _tag(args.get('tag_id') or args.get('id') or args.get('tag') or args.get('name'))
        if err:
            return {'error': err}, []
        n = _fetch('SELECT COUNT(*) n FROM entry_tags WHERE tag_id=?', (t['id'],))[0]['n']
        impact = f"标签「{t['name']}」会从 {n} 笔流水上摘掉（流水本身不删）"
        if not args.get('confirm'):
            return _need_confirm('删除标签', impact), []
        with A._write_lock:
            db.execute('DELETE FROM entry_tags WHERE tag_id=?', (t['id'],))
            db.execute('DELETE FROM tags WHERE id=?', (t['id'],))
            db.commit()
        res = {'ok': True, 'message': f"已删除标签「{t['name']}」，从 {n} 笔流水上摘除"}
        _audit('manage_tag', 'delete', args, res, sid)
        return res, []

    return {'error': 'action 只能是 create / update / delete'}, []


# ---------------------------------------------------------------- 定期存款
def _dep_fields(args, base=None):
    b = base or {}

    def pick(k, dflt=''):
        v = args.get(k)
        return v if v is not None else b.get(k, dflt)

    start = _parse_date(pick('start_date')) or (b.get('start_date') or '')
    end = _parse_date(pick('end_date')) or (b.get('end_date') or '')
    return {
        'bank': str(pick('bank') or '').strip(),
        'order_no': str(pick('order_no') or '').strip(),
        'currency': str(pick('currency') or b.get('currency') or 'HKD').upper(),
        'principal': _num(pick('principal'), b.get('principal') or 0),
        'period': str(pick('period') or '').strip(),
        'rate': _num(pick('rate'), b.get('rate') or 0),
        'start_date': start, 'end_date': end,
        'note': str(pick('note') or '').strip(),
    }


def t_manage_deposit(args):
    action = str(args.get('action') or '').strip().lower()
    sid = str(args.get('__sid') or '')
    A = _db()
    db = A.get_db()

    if action in ('create', 'add', '新建'):
        c, err = _company(args.get('company_id') or args.get('company'))
        if err:
            return {'error': err}, []
        f = _dep_fields(args)
        if not f['principal']:
            return {'error': '本金不能为 0'}, []
        cur = db.execute(
            'INSERT INTO deposits(company_id,bank,order_no,currency,principal,'
            'period,rate,start_date,end_date,note) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (c['id'], f['bank'], f['order_no'], f['currency'], f['principal'],
             f['period'], f['rate'], f['start_date'], f['end_date'], f['note']))
        db.commit()
        res = {'ok': True, 'id': cur.lastrowid,
               'message': (f"已在「{c['name']}」新建定期 {f['currency']} "
                           f"{f['principal']:,.2f}，到期 {f['end_date'] or '未填'}")}
        _audit('manage_deposit', 'create', args, res, sid)
        return res, []

    if action in ('update', '修改'):
        did = _opt_int(args.get('deposit_id') or args.get('id'))
        row = next(iter(_fetch('SELECT * FROM deposits WHERE id=?', (did,))), None) if did else None
        if not row:
            return {'error': '找不到这笔定期存款，先 list_deposits 拿 id'}, []
        f = _dep_fields(args, row)
        db.execute(
            'UPDATE deposits SET bank=?, order_no=?, currency=?, principal=?, '
            'period=?, rate=?, start_date=?, end_date=?, note=? WHERE id=?',
            (f['bank'], f['order_no'], f['currency'], f['principal'], f['period'],
             f['rate'], f['start_date'], f['end_date'], f['note'], did))
        db.commit()
        res = {'ok': True, 'id': did, 'message': f'定期存款 #{did} 已更新'}
        _audit('manage_deposit', 'update', args, res, sid)
        return res, []

    if action in ('delete', 'remove', '删除'):
        did = _opt_int(args.get('deposit_id') or args.get('id'))
        row = next(iter(_fetch('SELECT * FROM deposits WHERE id=?', (did,))), None) if did else None
        if not row:
            return {'error': '找不到这笔定期存款，先 list_deposits 拿 id'}, []
        impact = (f"定期 #{did} {row['currency']} {row['principal']:,.2f} "
                  f"（{row['start_date']}~{row['end_date']}）记录会被删除")
        if not args.get('confirm'):
            return _need_confirm('删除定期存款记录', impact), []
        db.execute('DELETE FROM deposits WHERE id=?', (did,))
        db.commit()
        res = {'ok': True, 'message': f'已删除定期存款 #{did}'}
        _audit('manage_deposit', 'delete', args, res, sid)
        return res, []

    return {'error': 'action 只能是 create / update / delete'}, []


# ---------------------------------------------------------------- 汇率
def t_set_rates(args):
    sid = str(args.get('__sid') or '')
    A = _db()
    db = A.get_db()
    raw = args.get('rates')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {'error': 'rates 要是 JSON 对象，如 {"HKD": 1.03, "CNY": 1.13}'}, []
    if isinstance(raw, dict):
        items = raw
    else:                       # 也支持 currency + rate 单条写法
        cur = str(args.get('currency') or '').strip().upper()
        if not cur:
            return {'error': '要么给 rates 对象，要么给 currency + rate'}, []
        items = {cur: args.get('rate')}
    done = {}
    with A._write_lock:
        for cur, rate in items.items():
            cur = str(cur or '').strip().upper()
            r = _num(rate)
            if not cur or r <= 0:
                continue
            db.execute('INSERT INTO rates(currency, rate) VALUES(?,?) '
                       'ON CONFLICT(currency) DO UPDATE SET rate=excluded.rate',
                       (cur, r))
            done[cur] = r
        db.commit()
    if not done:
        return {'error': '没有一个有效汇率（币种为空或汇率≤0）'}, []
    res = {'ok': True, 'rates': done,
           'message': '汇率已更新：' + '、'.join(f'{k}={v}' for k, v in done.items())}
    _audit('set_rates', 'set', args, res, sid)
    return res, []


# ---------------------------------------------------------------- 现金盘点
def t_manage_cash_count(args):
    action = str(args.get('action') or '').strip().lower()
    sid = str(args.get('__sid') or '')
    A = _db()
    db = A.get_db()

    def _items():
        raw = args.get('items') or []
        return [it for it in raw if isinstance(it, dict) and it.get('currency')]

    if action in ('create', 'add', '新建'):
        c, err = _company(args.get('company_id') or args.get('company'))
        if err:
            return {'error': err}, []
        day = _parse_date(args.get('count_date') or args.get('date'))
        if not day:
            return {'error': '盘点日期格式应为 YYYY-MM-DD'}, []
        items = _items()
        if not items:
            return {'error': '至少给一个币种的盘点行 items:[{currency, actual, ...}]'}, []
        with A._write_lock:
            cid = db.execute(
                'INSERT INTO cash_counts(company_id,count_date,person,supervisor,'
                'note,created_at) VALUES(?,?,?,?,?,?)',
                (c['id'], day, str(args.get('person') or ''),
                 str(args.get('supervisor') or ''), str(args.get('note') or ''),
                 datetime.now().strftime('%Y-%m-%d %H:%M:%S'))).lastrowid
            A._save_cash_items(db, cid, items)
            db.commit()
        res = {'ok': True, 'id': cid,
               'message': f"已录入「{c['name']}」{day} 的现金盘点单，{len(items)} 个币种"}
        _audit('manage_cash_count', 'create', args, res, sid)
        return res, []

    if action in ('update', '修改'):
        cid = _opt_int(args.get('count_id') or args.get('id'))
        row = next(iter(_fetch('SELECT * FROM cash_counts WHERE id=?', (cid,))), None) if cid else None
        if not row:
            return {'error': '找不到这张盘点单，先 list_cash_counts 拿 id'}, []
        day = _parse_date(args.get('count_date') or args.get('date')) or row['count_date']
        with A._write_lock:
            db.execute('UPDATE cash_counts SET count_date=?, person=?, supervisor=?, '
                       'note=? WHERE id=?',
                       (day, str(args.get('person') if args.get('person') is not None
                                 else row['person'] or ''),
                        str(args.get('supervisor') if args.get('supervisor') is not None
                            else row['supervisor'] or ''),
                        str(args.get('note') if args.get('note') is not None
                            else row['note'] or ''), cid))
            if args.get('items') is not None:
                A._save_cash_items(db, cid, _items())
            db.commit()
        res = {'ok': True, 'id': cid, 'message': f'盘点单 #{cid} 已更新'}
        _audit('manage_cash_count', 'update', args, res, sid)
        return res, []

    if action in ('delete', 'remove', '删除'):
        cid = _opt_int(args.get('count_id') or args.get('id'))
        row = next(iter(_fetch('SELECT * FROM cash_counts WHERE id=?', (cid,))), None) if cid else None
        if not row:
            return {'error': '找不到这张盘点单，先 list_cash_counts 拿 id'}, []
        impact = f"盘点单 #{cid}（{row['count_date']}）及其面额明细会被删除"
        if not args.get('confirm'):
            return _need_confirm('删除盘点单', impact), []
        with A._write_lock:
            db.execute('DELETE FROM cash_count_denoms WHERE item_id IN '
                       '(SELECT id FROM cash_count_items WHERE count_id=?)', (cid,))
            db.execute('DELETE FROM cash_count_items WHERE count_id=?', (cid,))
            db.execute('DELETE FROM cash_counts WHERE id=?', (cid,))
            db.commit()
        res = {'ok': True, 'message': f'已删除盘点单 #{cid}'}
        _audit('manage_cash_count', 'delete', args, res, sid)
        return res, []

    return {'error': 'action 只能是 create / update / delete'}, []


# ---------------------------------------------------------------- 工具定义
_ACTION = {'type': 'string', 'enum': ['create', 'update', 'delete'],
           'description': 'create=新建，update=修改，delete=删除'}

TOOLS = [
    {'type': 'function', 'function': {
        'name': 'system_info',
        'description': '系统概况：公司/账户/流水/标签/定期/盘点数量、数据库大小、'
                       '最近备份、当前 AI 权限策略。问「系统里有什么」「能操作哪些」'
                       '「有多少数据」时先调它。',
        'parameters': {'type': 'object', 'properties': {}, 'required': []}}},

    {'type': 'function', 'function': {
        'name': 'manage_company',
        'description': '新建 / 改名 / 删除公司。删除会级联删掉该公司所有账户、流水、'
                       '定期和盘点单，必须先向用户说明影响并得到明确同意，再带 '
                       'confirm=true 重调。',
        'parameters': {'type': 'object', 'properties': {
            'action': _ACTION,
            'company_id': {'type': 'integer', 'description': '改/删时定位公司'},
            'company': {'type': 'string', 'description': '公司名称，没有 id 时用'},
            'name': {'type': 'string', 'description': '新建或改名后的名称'},
        }, 'required': ['action']}}},

    {'type': 'function', 'function': {
        'name': 'manage_account',
        'description': '新建 / 修改 / 删除账户（银行户、库存现金户）。删除会级联删掉'
                       '该账户所有流水和附件，必须先向用户说明影响并得到明确同意，'
                       '再带 confirm=true 重调。改期初余额会改变所有余额，务必确认。',
        'parameters': {'type': 'object', 'properties': {
            'action': _ACTION,
            'account_id': {'type': 'integer', 'description': '改/删时定位账户'},
            'account': {'type': 'string', 'description': '账户名，没有 id 时用'},
            'name': {'type': 'string', 'description': '账户名称（新建必填）'},
            'company_id': {'type': 'integer'},
            'company': {'type': 'string', 'description': '公司名，新建时定位'},
            'currency': {'type': 'string', 'description': '币种，如 MOP / HKD / CNY'},
            'bank': {'type': 'string'},
            'account_no': {'type': 'string', 'description': '银行账号'},
            'opening': {'type': 'number', 'description': '期初余额'},
            'opening_date': {'type': 'string', 'description': '期初日期 YYYY-MM-DD'},
            'kind': {'type': 'string', 'enum': ['bank', 'cash'],
                     'description': 'bank=银行账户，cash=库存现金'},
        }, 'required': ['action']}}},

    {'type': 'function', 'function': {
        'name': 'manage_tag',
        'description': '新建 / 改名改色 / 删除标签。删除只把标签从流水上摘掉，不删流水。',
        'parameters': {'type': 'object', 'properties': {
            'action': _ACTION,
            'tag_id': {'type': 'integer'},
            'tag': {'type': 'string', 'description': '标签名'},
            'name': {'type': 'string', 'description': '新建时的标签名'},
            'new_name': {'type': 'string', 'description': '改名后的名称'},
            'color': {'type': 'string', 'description': '颜色，如 #ef4444'},
        }, 'required': ['action']}}},

    {'type': 'function', 'function': {
        'name': 'manage_deposit',
        'description': '新建 / 修改 / 删除定期存款记录。',
        'parameters': {'type': 'object', 'properties': {
            'action': _ACTION,
            'deposit_id': {'type': 'integer', 'description': '改/删时定位'},
            'company_id': {'type': 'integer'},
            'company': {'type': 'string'},
            'bank': {'type': 'string'},
            'order_no': {'type': 'string', 'description': '存单号'},
            'currency': {'type': 'string'},
            'principal': {'type': 'number', 'description': '本金'},
            'period': {'type': 'string', 'description': '存期，如「3个月」「1年」'},
            'rate': {'type': 'number', 'description': '年利率，填百分数本身，如 2.15'},
            'start_date': {'type': 'string', 'description': 'YYYY-MM-DD'},
            'end_date': {'type': 'string', 'description': '到期日 YYYY-MM-DD'},
            'note': {'type': 'string'},
        }, 'required': ['action']}}},

    {'type': 'function', 'function': {
        'name': 'set_exchange_rates',
        'description': '设置币种汇率（用于跨币种折算和盘点汇总）。',
        'parameters': {'type': 'object', 'properties': {
            'rates': {'type': 'object', 'description':
                      '形如 {"HKD": 1.03, "CNY": 1.13}，key 是币种，value 是相对本币的汇率'},
            'currency': {'type': 'string', 'description': '只设单个币种时用'},
            'rate': {'type': 'number'},
        }, 'required': []}}},

    {'type': 'function', 'function': {
        'name': 'manage_cash_count',
        'description': '录入 / 修改 / 删除库存现金盘点单。items 里每个币种一行。',
        'parameters': {'type': 'object', 'properties': {
            'action': _ACTION,
            'count_id': {'type': 'integer', 'description': '改/删时定位'},
            'company_id': {'type': 'integer'},
            'company': {'type': 'string'},
            'count_date': {'type': 'string', 'description': '盘点日期 YYYY-MM-DD'},
            'person': {'type': 'string', 'description': '盘点人'},
            'supervisor': {'type': 'string', 'description': '监盘人'},
            'note': {'type': 'string'},
            'items': {'type': 'array', 'description': '每个币种一行', 'items': {
                'type': 'object', 'properties': {
                    'currency': {'type': 'string'},
                    'prev_book': {'type': 'number', 'description': '上一日账面库存'},
                    'unposted_in': {'type': 'number', 'description': '未记账传票收入'},
                    'unposted_out': {'type': 'number', 'description': '未记账传票付出'},
                    'actual': {'type': 'number', 'description': '实有现金'},
                    'white': {'type': 'number', 'description': '白条抵库'},
                    'adj_paid': {'type': 'number', 'description': '报表日至盘点日付出'},
                    'report_book': {'type': 'number', 'description': '报表日应有余额'},
                    'reason': {'type': 'string', 'description': '差异原因'},
                    'denoms': {'type': 'array', 'description': '面额明细', 'items': {
                        'type': 'object', 'properties': {
                            'denom': {'type': 'number'},
                            'qty': {'type': 'number'}}}},
                }, 'required': ['currency']}},
        }, 'required': ['action']}}},

    {'type': 'function', 'function': {
        'name': 'backup_now',
        'description': '立刻把数据库和附件打包备份到 data/backups/。改账前想留个还原点'
                       '时用。恢复备份不开放给 AI，只能让用户在页面「设置」里操作。',
        'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
]

IMPL = {
    'system_info': t_system_info,
    'manage_company': t_manage_company,
    'manage_account': t_manage_account,
    'manage_tag': t_manage_tag,
    'manage_deposit': t_manage_deposit,
    'set_exchange_rates': t_set_rates,
    'manage_cash_count': t_manage_cash_count,
    'backup_now': t_backup_now,
}

LABELS = {
    'system_info': '读取系统概况',
    'manage_company': '维护公司',
    'manage_account': '维护账户',
    'manage_tag': '维护标签',
    'manage_deposit': '维护定期存款',
    'set_exchange_rates': '设置汇率',
    'manage_cash_count': '维护现金盘点',
    'backup_now': '备份数据库',
}
