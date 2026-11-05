# -*- coding: utf-8 -*-
"""日记账 BI 系统 —— 本地 Web 应用
启动：  python app.py   （自动打开浏览器 http://127.0.0.1:8765）
数据：  data/journal.db (SQLite) + data/uploads/ (凭证附件)
备份：  界面"设置"页可一键备份/恢复，或直接复制整个系统文件夹
"""
import io
import os
import re
import sys
import uuid
import shutil
import socket
import zipfile
import tempfile
import threading
import mimetypes
import webbrowser
import sqlite3
from datetime import date, datetime, timedelta

from flask import (Flask, g, request, jsonify, abort,
                   send_from_directory, send_file)

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
DB_PATH = os.path.join(DATA_DIR, 'journal.db')
os.makedirs(UPLOAD_DIR, exist_ok=True)

PORT = 8765
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'), static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB

# AI Agent（自然语言查账）：路由在 agent.py
from agent import bp as agent_bp  # noqa: E402
app.register_blueprint(agent_bp)

# AI 写入（草稿确认 / 撤销）
from agent_write import bp as agent_write_bp  # noqa: E402
app.register_blueprint(agent_write_bp)

# Agent 内核：流式对话（思考过程 + 工具调用 + 正文）、附件上传
from agent_core import bp as agent_core_bp  # noqa: E402
app.register_blueprint(agent_core_bp)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS companies(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL, name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'MOP', bank TEXT DEFAULT '',
  account_no TEXT DEFAULT '', opening REAL NOT NULL DEFAULT 0,
  sort INTEGER DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'bank',      -- bank 银行 / cash 库存现金
  opening_date TEXT DEFAULT '');          -- 期初余额所属日期（现金账户用）
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL, date TEXT NOT NULL,
  check_no TEXT DEFAULT '', description TEXT DEFAULT '', party TEXT DEFAULT '',
  debit REAL NOT NULL DEFAULT 0, credit REAL NOT NULL DEFAULT 0,
  pending INTEGER NOT NULL DEFAULT 0, project TEXT DEFAULT '',
  created_at TEXT);
CREATE TABLE IF NOT EXISTS entry_tags(
  entry_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, PRIMARY KEY(entry_id, tag_id));
CREATE TABLE IF NOT EXISTS tags(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '#3b82f6');
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id INTEGER NOT NULL,
  orig_name TEXT, stored TEXT, size INTEGER, mime TEXT, uploaded_at TEXT);
CREATE TABLE IF NOT EXISTS deposits(
  id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL,
  bank TEXT DEFAULT '', order_no TEXT DEFAULT '', currency TEXT DEFAULT 'HKD',
  principal REAL DEFAULT 0, period TEXT DEFAULT '', rate REAL DEFAULT 0,
  start_date TEXT DEFAULT '', end_date TEXT DEFAULT '', note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS rates(currency TEXT PRIMARY KEY, rate REAL NOT NULL);

-- 现金盘点单（一张单 = 某天对某个公司库存现金的盘点）
CREATE TABLE IF NOT EXISTS cash_counts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL,
  count_date TEXT NOT NULL,
  person TEXT DEFAULT '', supervisor TEXT DEFAULT '',
  note TEXT DEFAULT '', created_at TEXT);
-- 每个币种一行：账面应有 / 实有 / 差异
CREATE TABLE IF NOT EXISTS cash_count_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  count_id INTEGER NOT NULL, currency TEXT NOT NULL,
  prev_book REAL DEFAULT 0,      -- 上一日账面库存金额
  unposted_in REAL DEFAULT 0,    -- 盘点日未记账传票收入
  unposted_out REAL DEFAULT 0,   -- 盘点日未记账传票付出
  actual REAL DEFAULT 0,         -- 盘点实有现金（= 面额明细合计）
  white REAL DEFAULT 0,          -- 白条抵库
  reason TEXT DEFAULT '',        -- 差异原因分析
  adj_paid REAL DEFAULT 0,       -- 报表日至盘点日现金付出总额（+）
  report_book REAL DEFAULT 0);   -- 报表日库存现金应有余额
CREATE TABLE IF NOT EXISTS cash_count_denoms(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL, denom REAL NOT NULL, qty REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_cc_company ON cash_counts(company_id, count_date);
CREATE INDEX IF NOT EXISTS ix_cci_count ON cash_count_items(count_id);
CREATE INDEX IF NOT EXISTS ix_ccd_item ON cash_count_denoms(item_id);
'''

CASH_DENOMS = [1000, 500, 100, 50, 20, 10, 5, 2, 1, 0.5, 0.2, 0.1]


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.commit()
    con.close()


init_db()


def _migrate():
    con = sqlite3.connect(DB_PATH)
    cols = [r[1] for r in con.execute('PRAGMA table_info(entries)')]
    if 'note' not in cols:
        con.execute("ALTER TABLE entries ADD COLUMN note TEXT DEFAULT ''")
    acols = [r[1] for r in con.execute('PRAGMA table_info(accounts)')]
    if 'kind' not in acols:
        con.execute("ALTER TABLE accounts ADD COLUMN kind TEXT NOT NULL DEFAULT 'bank'")
    if 'opening_date' not in acols:
        con.execute("ALTER TABLE accounts ADD COLUMN opening_date TEXT DEFAULT ''")
    con.commit()
    con.close()


_migrate()

_write_lock = threading.Lock()
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


# ---------------------------------------------------------------- helpers
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def now_iso():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def bad(msg, code=400):
    resp = jsonify({'error': msg})
    resp.status_code = code
    return resp


def rowdict(r):
    return {k: r[k] for k in r.keys()}


def entry_tags_map(db, entry_ids):
    out = {}
    if not entry_ids:
        return out
    ph = ','.join('?' * len(entry_ids))
    rows = db.execute(
        f'SELECT et.entry_id, t.id, t.name, t.color FROM entry_tags et '
        f'JOIN tags t ON t.id = et.tag_id WHERE et.entry_id IN ({ph}) '
        f'ORDER BY t.id', entry_ids).fetchall()
    for r in rows:
        out.setdefault(r['entry_id'], []).append(
            {'id': r['id'], 'name': r['name'], 'color': r['color']})
    return out


def entry_att_map(db, entry_ids):
    out = {}
    if not entry_ids:
        return out
    ph = ','.join('?' * len(entry_ids))
    rows = db.execute(
        f'SELECT * FROM attachments WHERE entry_id IN ({ph}) ORDER BY id',
        entry_ids).fetchall()
    for r in rows:
        out.setdefault(r['entry_id'], []).append(rowdict(r))
    return out


def serialize_entries(db, rows):
    ids = [r['id'] for r in rows]
    tm = entry_tags_map(db, ids)
    am = entry_att_map(db, ids)
    out = []
    for r in rows:
        d = rowdict(r)
        d['tags'] = tm.get(r['id'], [])
        d['attachments'] = am.get(r['id'], [])
        out.append(d)
    return out


def set_entry_tags(db, entry_id, tag_ids):
    db.execute('DELETE FROM entry_tags WHERE entry_id=?', (entry_id,))
    for tid in tag_ids or []:
        db.execute('INSERT OR IGNORE INTO entry_tags(entry_id, tag_id) VALUES(?,?)',
                   (entry_id, int(tid)))


def validate_entry_payload(data, partial=False):
    """返回 (错误信息, 规范化字段)"""
    f = {}
    if not partial or 'date' in data:
        if not DATE_RE.match(str(data.get('date') or '')):
            return '日期格式应为 YYYY-MM-DD', None
        f['date'] = data['date']
    if not partial or 'account_id' in data:
        if not data.get('account_id'):
            return '缺少账户', None
        f['account_id'] = int(data['account_id'])
    for k in ('check_no', 'description', 'party', 'project', 'note'):
        if not partial or k in data:
            f[k] = str(data.get(k) or '').strip()
    debit = float(data.get('debit') or 0)
    credit = float(data.get('credit') or 0)
    if debit < 0 or credit < 0:
        return '金额不能为负', None
    if debit > 0 and credit > 0:
        return '收入与支出只能填一项', None
    if not partial or 'debit' in data:
        f['debit'] = debit
    if not partial or 'credit' in data:
        f['credit'] = credit
    if not partial or 'pending' in data:
        f['pending'] = 1 if data.get('pending') else 0
    return None, f


# ---------------------------------------------------------------- static
@app.get('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


# ---------------------------------------------------------------- bootstrap
@app.get('/api/bootstrap')
def bootstrap():
    db = get_db()
    companies = [rowdict(r) for r in db.execute(
        'SELECT * FROM companies ORDER BY sort, id')]
    accounts = [rowdict(r) for r in db.execute(
        'SELECT * FROM accounts ORDER BY company_id, sort, id')]
    tags = [rowdict(r) for r in db.execute('SELECT * FROM tags ORDER BY id')]
    rates = {r['currency']: r['rate'] for r in
             db.execute('SELECT * FROM rates')}
    counts = {'companies': len(companies), 'accounts': len(accounts),
              'entries': db.execute('SELECT COUNT(*) c FROM entries').fetchone()['c'],
              'attachments': db.execute('SELECT COUNT(*) c FROM attachments').fetchone()['c']}
    # 定期到期提醒（全部公司）：已到期 / 30天内到期
    today = date.today()
    due = soon = 0
    for d in db.execute("SELECT end_date FROM deposits WHERE end_date<>''"):
        try:
            left = (datetime.strptime(d['end_date'], '%Y-%m-%d').date() - today).days
        except ValueError:
            continue
        if left < 0:
            due += 1
        elif left <= 30:
            soon += 1
    rng = db.execute("SELECT MIN(date) mn, MAX(date) mx FROM entries "
                     "WHERE date IS NOT NULL AND date<>''").fetchone()
    # 每家公司的存单数（导出弹窗据此决定是否显示「定期存款」页的勾选）
    dep_counts = {str(r['company_id']): r['n'] for r in db.execute(
        'SELECT company_id, COUNT(*) n FROM deposits GROUP BY company_id')}
    # 每家公司的现金盘点单数（现金页/导出弹窗用）
    cash_count_n = {str(r['company_id']): r['n'] for r in db.execute(
        'SELECT company_id, COUNT(*) n FROM cash_counts GROUP BY company_id')}
    return jsonify({'companies': companies, 'accounts': accounts,
                    'tags': tags, 'rates': rates, 'counts': counts,
                    'dep_alerts': {'due': due, 'soon': soon},
                    'dep_counts': dep_counts,
                    'cash_counts': cash_count_n,
                    'entry_range': {'min': rng['mn'] if rng else None,
                                    'max': rng['mx'] if rng else None},
                    'today': today.isoformat()})


# ---------------------------------------------------------------- companies
@app.post('/api/companies')
def add_company():
    data = request.get_json(force=True)
    name = str(data.get('name') or '').strip()
    if not name:
        return bad('公司名称不能为空')
    db = get_db()
    cur = db.execute('INSERT INTO companies(name) VALUES(?)', (name,))
    db.commit()
    return jsonify({'id': cur.lastrowid, 'name': name})


@app.put('/api/companies/<int:cid>')
def upd_company(cid):
    data = request.get_json(force=True)
    name = str(data.get('name') or '').strip()
    if not name:
        return bad('公司名称不能为空')
    db = get_db()
    db.execute('UPDATE companies SET name=? WHERE id=?', (name, cid))
    db.commit()
    return jsonify({'ok': True})


@app.delete('/api/companies/<int:cid>')
def del_company(cid):
    db = get_db()
    with _write_lock:
        acc_ids = [r['id'] for r in db.execute(
            'SELECT id FROM accounts WHERE company_id=?', (cid,))]
        for aid in acc_ids:
            _delete_account_db(db, aid)
        db.execute('DELETE FROM deposits WHERE company_id=?', (cid,))
        db.execute('DELETE FROM cash_count_denoms WHERE item_id IN '
                   '(SELECT id FROM cash_count_items WHERE count_id IN '
                   '(SELECT id FROM cash_counts WHERE company_id=?))', (cid,))
        db.execute('DELETE FROM cash_count_items WHERE count_id IN '
                   '(SELECT id FROM cash_counts WHERE company_id=?)', (cid,))
        db.execute('DELETE FROM cash_counts WHERE company_id=?', (cid,))
        db.execute('DELETE FROM companies WHERE id=?', (cid,))
        db.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------- accounts
@app.post('/api/accounts')
def add_account():
    data = request.get_json(force=True)
    name = str(data.get('name') or '').strip()
    if not name:
        return bad('账户名称不能为空')
    db = get_db()
    if not db.execute('SELECT 1 FROM companies WHERE id=?',
                      (data.get('company_id'),)).fetchone():
        return bad('公司不存在')
    kind = 'cash' if str(data.get('kind') or 'bank') == 'cash' else 'bank'
    cur = db.execute(
        'INSERT INTO accounts(company_id,name,currency,bank,account_no,opening,'
        'kind,opening_date) VALUES(?,?,?,?,?,?,?,?)',
        (data.get('company_id'), name, str(data.get('currency') or 'MOP'),
         str(data.get('bank') or ''), str(data.get('account_no') or ''),
         float(data.get('opening') or 0), kind,
         str(data.get('opening_date') or '')))
    db.commit()
    return jsonify({'id': cur.lastrowid})


@app.put('/api/accounts/<int:aid>')
def upd_account(aid):
    data = request.get_json(force=True)
    db = get_db()
    row = db.execute('SELECT * FROM accounts WHERE id=?', (aid,)).fetchone()
    if not row:
        return bad('账户不存在', 404)
    name = str(data.get('name') or row['name']).strip() or row['name']
    kind = str(data.get('kind') or row['kind'] or 'bank')
    if kind not in ('bank', 'cash'):
        kind = 'bank'
    db.execute(
        'UPDATE accounts SET name=?, currency=?, bank=?, account_no=?, opening=?, '
        'kind=?, opening_date=? WHERE id=?',
        (name, str(data.get('currency') or row['currency']),
         str(data.get('bank') if data.get('bank') is not None else row['bank']),
         str(data.get('account_no') if data.get('account_no') is not None else row['account_no']),
         float(data.get('opening') if data.get('opening') is not None else row['opening']),
         kind,
         str(data.get('opening_date') if data.get('opening_date') is not None
             else (row['opening_date'] or '')),
         aid))
    db.commit()
    return jsonify({'ok': True})


def _delete_account_db(db, aid):
    """级联删除账户（附件文件、标签关联、流水）"""
    for r in db.execute('SELECT * FROM attachments WHERE entry_id IN '
                        '(SELECT id FROM entries WHERE account_id=?)', (aid,)):
        p = os.path.join(UPLOAD_DIR, r['stored'])
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    db.execute('DELETE FROM entry_tags WHERE entry_id IN '
               '(SELECT id FROM entries WHERE account_id=?)', (aid,))
    db.execute('DELETE FROM attachments WHERE entry_id IN '
               '(SELECT id FROM entries WHERE account_id=?)', (aid,))
    db.execute('DELETE FROM entries WHERE account_id=?', (aid,))
    db.execute('DELETE FROM accounts WHERE id=?', (aid,))


@app.delete('/api/accounts/<int:aid>')
def del_account(aid):
    db = get_db()
    with _write_lock:
        _delete_account_db(db, aid)
        db.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------- entries
@app.get('/api/entries')
def list_entries():
    aid = request.args.get('account_id', type=int)
    if not aid:
        return bad('缺少 account_id')
    db = get_db()
    acc = db.execute('SELECT * FROM accounts WHERE id=?', (aid,)).fetchone()
    if not acc:
        return bad('账户不存在', 404)
    rows = db.execute('SELECT * FROM entries WHERE account_id=? '
                      'ORDER BY date, id', (aid,)).fetchall()
    return jsonify({'account': rowdict(acc), 'entries': serialize_entries(db, rows)})


@app.post('/api/entries')
def add_entry():
    data = request.get_json(force=True)
    err, f = validate_entry_payload(data)
    if err:
        return bad(err)
    db = get_db()
    with _write_lock:
        cur = db.execute(
            'INSERT INTO entries(account_id,date,check_no,description,party,'
            'debit,credit,pending,project,note,created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (f['account_id'], f['date'], f['check_no'], f['description'],
             f['party'], f['debit'], f['credit'], f['pending'], f['project'],
             f['note'], now_iso()))
        set_entry_tags(db, cur.lastrowid, data.get('tag_ids'))
        db.commit()
        eid = cur.lastrowid
    row = db.execute('SELECT * FROM entries WHERE id=?', (eid,)).fetchone()
    return jsonify(serialize_entries(db, [row])[0])


@app.put('/api/entries/<int:eid>')
def upd_entry(eid):
    data = request.get_json(force=True)
    db = get_db()
    row = db.execute('SELECT * FROM entries WHERE id=?', (eid,)).fetchone()
    if not row:
        return bad('流水不存在', 404)
    err, f = validate_entry_payload(data, partial=True)
    if err:
        return bad(err)
    merged = dict(row)
    merged.update(f)
    with _write_lock:
        db.execute(
            'UPDATE entries SET date=?, check_no=?, description=?, party=?, '
            'debit=?, credit=?, pending=?, project=?, note=? WHERE id=?',
            (merged['date'], merged['check_no'], merged['description'],
             merged['party'], merged['debit'], merged['credit'],
             merged['pending'], merged['project'], merged['note'], eid))
        if 'tag_ids' in data:
            set_entry_tags(db, eid, data.get('tag_ids'))
        db.commit()
    row = db.execute('SELECT * FROM entries WHERE id=?', (eid,)).fetchone()
    return jsonify(serialize_entries(db, [row])[0])


@app.post('/api/entries/<int:eid>/clear-marks')
def clear_entry_marks(eid):
    """清空该笔流水的备注与标签（提醒弹窗中确认“不再提示”时调用）"""
    db = get_db()
    if not db.execute('SELECT 1 FROM entries WHERE id=?', (eid,)).fetchone():
        return bad('流水不存在')
    with _write_lock:
        db.execute("UPDATE entries SET note='' WHERE id=?", (eid,))
        db.execute('DELETE FROM entry_tags WHERE entry_id=?', (eid,))
        db.commit()
    return jsonify({'ok': True})


@app.delete('/api/entries/<int:eid>')
def del_entry(eid):
    db = get_db()
    with _write_lock:
        for r in db.execute('SELECT * FROM attachments WHERE entry_id=?', (eid,)):
            p = os.path.join(UPLOAD_DIR, r['stored'])
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        db.execute('DELETE FROM entry_tags WHERE entry_id=?', (eid,))
        db.execute('DELETE FROM attachments WHERE entry_id=?', (eid,))
        db.execute('DELETE FROM entries WHERE id=?', (eid,))
        db.commit()
    return jsonify({'ok': True})


@app.get('/api/notes/reminders')
def note_reminders():
    """全部带备注或标签（待跟进）的流水，供侧栏提醒弹窗使用"""
    db = get_db()
    rows = db.execute(
        "SELECT e.id, e.account_id, e.date, e.description, e.note, "
        "a.name AS account_name, a.currency, c.id AS company_id, "
        "c.name AS company_name "
        "FROM entries e JOIN accounts a ON a.id = e.account_id "
        "JOIN companies c ON c.id = a.company_id "
        "WHERE TRIM(e.note) <> '' "
        "   OR e.id IN (SELECT entry_id FROM entry_tags) "
        "ORDER BY e.date, e.id").fetchall()
    tm = entry_tags_map(db, [r['id'] for r in rows])
    out = []
    for r in rows:
        d = rowdict(r)
        d['tags'] = tm.get(r['id'], [])
        out.append(d)
    return jsonify(out)


@app.put('/api/entries/<int:eid>/pending')
def toggle_pending(eid):
    data = request.get_json(force=True)
    db = get_db()
    db.execute('UPDATE entries SET pending=? WHERE id=?',
               (1 if data.get('pending') else 0, eid))
    db.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------- attachments
@app.post('/api/entries/<int:eid>/attachments')
def upload_attachments(eid):
    db = get_db()
    if not db.execute('SELECT 1 FROM entries WHERE id=?', (eid,)).fetchone():
        return bad('流水不存在', 404)
    files = request.files.getlist('files')
    if not files:
        return bad('未选择文件')
    saved = []
    with _write_lock:
        for f in files:
            orig = os.path.basename(f.filename or '未命名')
            ext = os.path.splitext(orig)[1][:16]
            stored = uuid.uuid4().hex + ext
            f.save(os.path.join(UPLOAD_DIR, stored))
            mime = mimetypes.guess_type(orig)[0] or 'application/octet-stream'
            cur = db.execute(
                'INSERT INTO attachments(entry_id,orig_name,stored,size,mime,uploaded_at) '
                'VALUES(?,?,?,?,?,?)',
                (eid, orig, stored, os.path.getsize(os.path.join(UPLOAD_DIR, stored)),
                 mime, now_iso()))
            saved.append(cur.lastrowid)
        db.commit()
    rows = db.execute(
        f'SELECT * FROM attachments WHERE id IN ({",".join("?"*len(saved))})',
        saved).fetchall()
    return jsonify([rowdict(r) for r in rows])


@app.get('/api/attachments/<int:attid>/file')
def attachment_file(attid):
    db = get_db()
    r = db.execute('SELECT * FROM attachments WHERE id=?', (attid,)).fetchone()
    if not r:
        abort(404)
    as_att = request.args.get('download') == '1'
    return send_from_directory(
        UPLOAD_DIR, r['stored'], as_attachment=as_att,
        download_name=r['orig_name'] if as_att else None,
        mimetype=r['mime'] or 'application/octet-stream')


@app.delete('/api/attachments/<int:attid>')
def del_attachment(attid):
    db = get_db()
    r = db.execute('SELECT * FROM attachments WHERE id=?', (attid,)).fetchone()
    if not r:
        return bad('附件不存在', 404)
    with _write_lock:
        p = os.path.join(UPLOAD_DIR, r['stored'])
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
        db.execute('DELETE FROM attachments WHERE id=?', (attid,))
        db.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------- tags
@app.get('/api/tags')
def list_tags():
    db = get_db()
    rows = db.execute(
        'SELECT t.*, COUNT(et.entry_id) usage_count FROM tags t '
        'LEFT JOIN entry_tags et ON et.tag_id=t.id '
        'GROUP BY t.id ORDER BY t.id').fetchall()
    return jsonify([rowdict(r) for r in rows])


@app.post('/api/tags')
def add_tag():
    data = request.get_json(force=True)
    name = str(data.get('name') or '').strip()
    color = str(data.get('color') or '#3b82f6')
    if not re.match(r'^#[0-9a-fA-F]{6}$', color):
        return bad('颜色格式不正确')
    if not name:
        return bad('标签名称不能为空')
    db = get_db()
    cur = db.execute('INSERT INTO tags(name, color) VALUES(?,?)', (name, color))
    db.commit()
    return jsonify({'id': cur.lastrowid, 'name': name, 'color': color})


@app.put('/api/tags/<int:tid>')
def upd_tag(tid):
    data = request.get_json(force=True)
    name = str(data.get('name') or '').strip()
    color = str(data.get('color') or '')
    db = get_db()
    if name:
        db.execute('UPDATE tags SET name=? WHERE id=?', (name, tid))
    if re.match(r'^#[0-9a-fA-F]{6}$', color):
        db.execute('UPDATE tags SET color=? WHERE id=?', (color, tid))
    db.commit()
    return jsonify({'ok': True})


@app.delete('/api/tags/<int:tid>')
def del_tag(tid):
    db = get_db()
    with _write_lock:
        db.execute('DELETE FROM entry_tags WHERE tag_id=?', (tid,))
        db.execute('DELETE FROM tags WHERE id=?', (tid,))
        db.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------- deposits (定期)
@app.get('/api/deposits')
def list_deposits():
    cid = request.args.get('company_id', type=int)
    if not cid:
        return bad('缺少 company_id')
    db = get_db()
    rows = db.execute('SELECT * FROM deposits WHERE company_id=? '
                      'ORDER BY start_date, id', (cid,)).fetchall()
    return jsonify([rowdict(r) for r in rows])


def _deposit_fields(data):
    def fnum(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d
    start = str(data.get('start_date') or '')
    end = str(data.get('end_date') or '')
    if start and not DATE_RE.match(start):
        return None, '开始日期格式应为 YYYY-MM-DD'
    if end and not DATE_RE.match(end):
        return None, '到期日格式应为 YYYY-MM-DD'
    return {
        'bank': str(data.get('bank') or '').strip(),
        'order_no': str(data.get('order_no') or '').strip(),
        'currency': str(data.get('currency') or 'HKD'),
        'principal': fnum(data.get('principal')),
        'period': str(data.get('period') or '').strip(),
        'rate': fnum(data.get('rate')),
        'start_date': start, 'end_date': end,
        'note': str(data.get('note') or '').strip(),
    }, None


@app.post('/api/deposits')
def add_deposit():
    data = request.get_json(force=True)
    f, err = _deposit_fields(data)
    if err:
        return bad(err)
    db = get_db()
    cur = db.execute(
        'INSERT INTO deposits(company_id,bank,order_no,currency,principal,'
        'period,rate,start_date,end_date,note) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (data.get('company_id'), f['bank'], f['order_no'], f['currency'],
         f['principal'], f['period'], f['rate'], f['start_date'],
         f['end_date'], f['note']))
    db.commit()
    return jsonify({'id': cur.lastrowid})


@app.put('/api/deposits/<int:did>')
def upd_deposit(did):
    data = request.get_json(force=True)
    f, err = _deposit_fields(data)
    if err:
        return bad(err)
    db = get_db()
    db.execute(
        'UPDATE deposits SET bank=?, order_no=?, currency=?, principal=?, '
        'period=?, rate=?, start_date=?, end_date=?, note=? WHERE id=?',
        (f['bank'], f['order_no'], f['currency'], f['principal'], f['period'],
         f['rate'], f['start_date'], f['end_date'], f['note'], did))
    db.commit()
    return jsonify({'ok': True})


@app.delete('/api/deposits/<int:did>')
def del_deposit(did):
    db = get_db()
    db.execute('DELETE FROM deposits WHERE id=?', (did,))
    db.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------- rates
@app.put('/api/rates')
def set_rates():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return bad('格式错误')
    db = get_db()
    with _write_lock:
        for cur, rate in data.items():
            try:
                r = float(rate)
            except (TypeError, ValueError):
                continue
            if r <= 0:
                continue
            db.execute('INSERT INTO rates(currency, rate) VALUES(?,?) '
                       'ON CONFLICT(currency) DO UPDATE SET rate=excluded.rate',
                       (str(cur).strip().upper(), r))
        db.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------- 库存现金 / 盘点
def _cash_book_at(db, company_id, upto=''):
    """该公司各现金账户截至 upto（含当日）的账面余额，按币种汇总"""
    rows = db.execute(
        "SELECT * FROM accounts WHERE company_id=? AND kind='cash' "
        "ORDER BY sort, id", (company_id,)).fetchall()
    out = []
    for a in rows:
        sql = ('SELECT COALESCE(SUM(debit),0) d, COALESCE(SUM(credit),0) c, '
               'COUNT(*) n FROM entries WHERE account_id=? AND pending=0')
        params = [a['id']]
        if upto:
            sql += ' AND date<=?'
            params.append(upto)
        agg = db.execute(sql, params).fetchone()
        book = a['opening'] + agg['d'] - agg['c']
        out.append({**rowdict(a), 'book': book, 'entry_count': agg['n']})
    return out


def _denoms_of(db, item_id):
    return [dict(r) for r in db.execute(
        'SELECT denom, qty FROM cash_count_denoms WHERE item_id=? '
        'ORDER BY denom DESC', (item_id,))]


def _cash_item_calc(it, denoms):
    """统一口径：实有 = 面额合计（有明细时），账面应有 = 上一日 + 未记账收入 - 未记账付出"""
    amt = round(sum((d['denom'] or 0) * (d['qty'] or 0) for d in denoms), 2)
    actual = amt if denoms else round(float(it.get('actual') or 0), 2)
    book = round((it.get('prev_book') or 0) + (it.get('unposted_in') or 0)
                 - (it.get('unposted_out') or 0), 2)
    return {'actual': actual, 'book': book,
            'diff': round(actual - book, 2),
            'white': round(float(it.get('white') or 0), 2)}


def _cash_count_payload(db, cid):
    head = db.execute('SELECT * FROM cash_counts WHERE id=?', (cid,)).fetchone()
    if not head:
        return None
    items = []
    for it in db.execute('SELECT * FROM cash_count_items WHERE count_id=? '
                         'ORDER BY id', (cid,)):
        ds = _denoms_of(db, it['id'])
        calc = _cash_item_calc(rowdict(it), ds)
        items.append({**rowdict(it), **calc, 'denoms': ds})
    return {'count': rowdict(head), 'items': items}


@app.get('/api/cash/book')
def cash_book():
    cid = request.args.get('company_id', type=int)
    if not cid:
        return bad('缺少 company_id')
    d = (request.args.get('date') or '').strip()
    if d and not DATE_RE.match(d):
        return bad('日期格式应为 YYYY-MM-DD')
    db = get_db()
    accs = _cash_book_at(db, cid, d)
    return jsonify({'accounts': accs, 'date': d,
                    'total_local': sum(a['book'] for a in accs)})


@app.get('/api/cash/counts')
def list_cash_counts():
    cid = request.args.get('company_id', type=int)
    if not cid:
        return bad('缺少 company_id')
    db = get_db()
    rates = {r['currency']: r['rate'] for r in db.execute('SELECT * FROM rates')}
    out = []
    for h in db.execute('SELECT * FROM cash_counts WHERE company_id=? '
                        'ORDER BY count_date DESC, id DESC', (cid,)):
        items = []
        for it in db.execute('SELECT * FROM cash_count_items WHERE count_id=? '
                             'ORDER BY id', (h['id'],)):
            calc = _cash_item_calc(rowdict(it), _denoms_of(db, it['id']))
            items.append({**rowdict(it), **calc})
        fx = lambda cur: rates.get(cur, 1) or 1
        out.append({**rowdict(h),
                    'items': items,
                    'currencies': '/'.join(i['currency'] for i in items),
                    'book_total': round(sum(i['book'] * fx(i['currency']) for i in items), 2),
                    'actual_total': round(sum(i['actual'] * fx(i['currency']) for i in items), 2),
                    'diff_total': round(sum(i['diff'] * fx(i['currency']) for i in items), 2),
                    'white_total': round(sum(i['white'] * fx(i['currency']) for i in items), 2)})
    return jsonify(out)


@app.get('/api/cash/counts/<int:cid>')
def get_cash_count(cid):
    data = _cash_count_payload(get_db(), cid)
    if not data:
        return bad('盘点单不存在', 404)
    return jsonify(data)


def _save_cash_items(db, cid, items):
    db.execute('DELETE FROM cash_count_denoms WHERE item_id IN '
               '(SELECT id FROM cash_count_items WHERE count_id=?)', (cid,))
    db.execute('DELETE FROM cash_count_items WHERE count_id=?', (cid,))
    for it in items:
        cur = str(it.get('currency') or '').upper().strip()
        if not cur:
            continue
        denoms = []
        for d in (it.get('denoms') or []):
            try:
                dv, qv = float(d.get('denom') or 0), float(d.get('qty') or 0)
            except (TypeError, ValueError):
                continue
            if dv and qv:
                denoms.append({'denom': dv, 'qty': qv})
        calc = _cash_item_calc(it, denoms)
        item_id = db.execute(
            'INSERT INTO cash_count_items(count_id,currency,prev_book,unposted_in,'
            'unposted_out,actual,white,reason,adj_paid,report_book) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (cid, cur, float(it.get('prev_book') or 0),
             float(it.get('unposted_in') or 0), float(it.get('unposted_out') or 0),
             calc['actual'], float(it.get('white') or 0),
             str(it.get('reason') or ''), float(it.get('adj_paid') or 0),
             float(it.get('report_book') or 0))).lastrowid
        for d in denoms:
            db.execute('INSERT INTO cash_count_denoms(item_id,denom,qty) '
                       'VALUES(?,?,?)', (item_id, d['denom'], d['qty']))


@app.post('/api/cash/counts')
def add_cash_count():
    data = request.get_json(force=True)
    cid_company = data.get('company_id')
    day = str(data.get('count_date') or '').strip()
    if not cid_company:
        return bad('缺少 company_id')
    if not DATE_RE.match(day):
        return bad('盘点日期格式应为 YYYY-MM-DD')
    items = data.get('items') or []
    if not items:
        return bad('至少要有一个币种的盘点记录')
    db = get_db()
    try:
        with _write_lock:
            cid = db.execute(
                'INSERT INTO cash_counts(company_id,count_date,person,supervisor,'
                'note,created_at) VALUES(?,?,?,?,?,?)',
                (cid_company, day, str(data.get('person') or ''),
                 str(data.get('supervisor') or ''), str(data.get('note') or ''),
                 now_iso())).lastrowid
            _save_cash_items(db, cid, items)
            db.commit()
    except sqlite3.Error as e:
        db.rollback()
        return bad(f'保存失败：{e}', 500)
    return jsonify({'id': cid})


@app.put('/api/cash/counts/<int:cid>')
def upd_cash_count(cid):
    data = request.get_json(force=True)
    day = str(data.get('count_date') or '').strip()
    if day and not DATE_RE.match(day):
        return bad('盘点日期格式应为 YYYY-MM-DD')
    db = get_db()
    row = db.execute('SELECT * FROM cash_counts WHERE id=?', (cid,)).fetchone()
    if not row:
        return bad('盘点单不存在', 404)
    try:
        with _write_lock:
            db.execute(
                'UPDATE cash_counts SET count_date=?, person=?, supervisor=?, note=? '
                'WHERE id=?',
                (day or row['count_date'],
                 str(data.get('person') or ''), str(data.get('supervisor') or ''),
                 str(data.get('note') or ''), cid))
            if data.get('items') is not None:
                _save_cash_items(db, cid, data.get('items') or [])
            db.commit()
    except sqlite3.Error as e:
        db.rollback()
        return bad(f'保存失败：{e}', 500)
    return jsonify({'ok': True})


@app.delete('/api/cash/counts/<int:cid>')
def del_cash_count(cid):
    db = get_db()
    with _write_lock:
        db.execute('DELETE FROM cash_count_denoms WHERE item_id IN '
                   '(SELECT id FROM cash_count_items WHERE count_id=?)', (cid,))
        db.execute('DELETE FROM cash_count_items WHERE count_id=?', (cid,))
        db.execute('DELETE FROM cash_counts WHERE id=?', (cid,))
        db.commit()
    return jsonify({'ok': True})


@app.get('/api/export/cash-counts')
def export_cash_counts():
    """盘点表导出（Excel）
    参数：company_id 必填；from / to 可选（按盘点日期过滤）；id 可选（只导这一张）
    三个工作表：盘点汇总（折 MOP）／盘点明细（原币）／面额明细（每张单一个块）
    """
    import io as _io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties

    cid = request.args.get('company_id', type=int)
    if not cid:
        return bad('缺少 company_id')
    frm = (request.args.get('from') or '').strip()
    to = (request.args.get('to') or '').strip()
    only_id = request.args.get('id', type=int)
    for d in (frm, to):
        if not d:
            continue
        try:
            datetime.strptime(d, '%Y-%m-%d')
        except ValueError:
            return bad('日期格式应为 YYYY-MM-DD（且必须是真实日期）')

    db = get_db()
    cp = db.execute('SELECT * FROM companies WHERE id=?', (cid,)).fetchone()
    if not cp:
        return bad('公司不存在', 404)
    rates = {r['currency']: r['rate'] for r in db.execute('SELECT * FROM rates')}
    fx = lambda cur: (rates.get(cur) or 1)
    company = cp['name']

    sql = 'SELECT * FROM cash_counts WHERE company_id=?'
    args = [cid]
    if only_id:
        sql += ' AND id=?'
        args.append(only_id)
    if frm:
        sql += ' AND count_date>=?'
        args.append(frm)
    if to:
        sql += ' AND count_date<=?'
        args.append(to)
    sql += ' ORDER BY count_date, id'
    heads = db.execute(sql, args).fetchall()

    blocks = []                      # [(head, [item+calc+denoms])]
    for h in heads:
        items = []
        for it in db.execute('SELECT * FROM cash_count_items WHERE count_id=? '
                             'ORDER BY id', (h['id'],)):
            ds = _denoms_of(db, it['id'])
            items.append({**rowdict(it), **_cash_item_calc(rowdict(it), ds),
                          'denoms': ds})
        if items:
            blocks.append((rowdict(h), items))

    # ---------- 样式
    FN = '微软雅黑'
    thin = Side(style='thin', color='BFBFBF')
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    base = Font(name=FN, size=10)
    bold = Font(name=FN, size=10, bold=True)
    right = Alignment(horizontal='right', vertical='center')
    ctr = Alignment(horizontal='center', vertical='center')
    wrap = Alignment(horizontal='left', vertical='center', wrap_text=True)
    money = '#,##0.00'

    def head_row(ws, r, cols, widths=None, fill='4472C4'):
        for i, t in enumerate(cols, 1):
            c = ws.cell(r, i, t)
            c.font = Font(name=FN, size=10, bold=True, color='FFFFFF')
            c.fill = PatternFill('solid', fgColor=fill)
            c.alignment = ctr
            c.border = box
        ws.row_dimensions[r].height = 26
        if widths:
            for i, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(i)].width = w

    def title_row(ws, r, text, ncols, size=14):
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)
        c = ws.cell(r, 1, text)
        c.font = Font(name=FN, size=size, bold=True, color='1F4E79')
        c.alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[r].height = 28

    wb = Workbook()

    # ========== Sheet1 盘点汇总（一行一张单，折 MOP） ==========
    ws = wb.active
    ws.title = '盘点汇总'
    ws.sheet_view.showGridLines = False
    ncol = 10
    title_row(ws, 1, f'{company}　现金盘点表', ncol)
    title_row(ws, 2, f'期间：{frm or "不限"} ~ {to or "不限"}　　币种金额已按设置汇率折 MOP', ncol, 9)
    head_row(ws, 4, ['盘点日期', '币种', '账面应有(折MOP)', '盘点实有(折MOP)',
                     '差异(折MOP)', '白条抵库(折MOP)', '结果', '盘点人', '监盘人', '备注'],
             [13, 12, 17, 17, 15, 16, 10, 12, 12, 28])
    r = 5
    s_book = s_act = s_diff = s_white = 0.0
    for h, items in blocks:
        book = sum(i['book'] * fx(i['currency']) for i in items)
        act = sum(i['actual'] * fx(i['currency']) for i in items)
        white = sum(i['white'] * fx(i['currency']) for i in items)
        diff = act - book
        s_book += book
        s_act += act
        s_diff += diff
        s_white += white
        cur_txt = '/'.join(dict.fromkeys(i['currency'] for i in items))
        vals = [h['count_date'], cur_txt, book, act, diff, white,
                '盘盈' if diff > 0.005 else ('盘亏' if diff < -0.005 else '相符'),
                h['person'] or '', h['supervisor'] or '', h['note'] or '']
        for c, v in enumerate(vals, 1):
            cell = ws.cell(r, c, v)
            cell.border = box
            cell.font = base
            cell.alignment = wrap if c == 10 else (right if c in (3, 4, 5, 6) else ctr)
            if c in (3, 4, 5, 6):
                cell.number_format = money
            if c == 5 and abs(diff) > 0.005:
                cell.font = Font(name=FN, size=10, bold=True,
                                 color='C0392B' if diff < 0 else '19753C')
        r += 1
    if not blocks:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
        c = ws.cell(r, 1, '该期间没有盘点记录')
        c.font = base
        c.alignment = ctr
        r += 1
    # 合计
    ws.cell(r, 1, '合计（折 MOP）').font = bold
    ws.cell(r, 1).alignment = ctr
    ws.cell(r, 1).border = box
    for c in (2, 7, 8, 9, 10):
        ws.cell(r, c).border = box
    for c, v in ((3, s_book), (4, s_act), (5, s_diff), (6, s_white)):
        cell = ws.cell(r, c, round(v, 2))
        cell.font = bold
        cell.border = box
        cell.alignment = right
        cell.number_format = money
        cell.fill = PatternFill('solid', fgColor='F2F2F2')
    for c in (1, 2, 7, 8, 9, 10):
        ws.cell(r, c).fill = PatternFill('solid', fgColor='F2F2F2')
    ws.freeze_panes = 'A5'

    # ========== Sheet2 盘点明细（原币逐币种） ==========
    ws2 = wb.create_sheet('盘点明细')
    ws2.sheet_view.showGridLines = False
    cols2 = ['盘点日期', '币种', '上一日账面库存', '未记账收入', '未记账付出',
             '账面应有', '盘点实有', '差异', '白条抵库', '差异原因', '盘点人', '监盘人']
    title_row(ws2, 1, f'{company}　盘点明细（原币）', len(cols2))
    head_row(ws2, 3, cols2, [13, 8, 15, 13, 13, 14, 14, 13, 12, 30, 11, 11])
    r = 4
    for h, items in blocks:
        for it in items:
            vals = [h['count_date'], it['currency'], it['prev_book'],
                    it['unposted_in'], it['unposted_out'], it['book'],
                    it['actual'], it['diff'], it['white'], it['reason'] or '',
                    h['person'] or '', h['supervisor'] or '']
            for c, v in enumerate(vals, 1):
                cell = ws2.cell(r, c, v)
                cell.border = box
                cell.font = base
                cell.alignment = wrap if c == 10 else (right if 3 <= c <= 9 else ctr)
                if 3 <= c <= 9:
                    cell.number_format = money
                if c == 8 and abs(it['diff']) > 0.005:
                    cell.font = Font(name=FN, size=10, bold=True, color='C0392B')
            r += 1
    ws2.freeze_panes = 'A4'

    # ========== Sheet3 面额明细（每张单一个块） ==========
    ws3 = wb.create_sheet('面额明细')
    ws3.sheet_view.showGridLines = False
    ncol3 = len(CASH_DENOMS) + 3
    title_row(ws3, 1, f'{company}　面额明细（张数 × 面额）', ncol3)
    ws3.column_dimensions['A'].width = 13
    ws3.column_dimensions['B'].width = 8
    for i in range(3, 2 + len(CASH_DENOMS) + 1):
        ws3.column_dimensions[get_column_letter(i)].width = 8
    ws3.column_dimensions[get_column_letter(len(CASH_DENOMS) + 4)].width = 14
    r = 3
    for h, items in blocks:
        ws3.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol3)
        tc = ws3.cell(r, 1, f'▍{h["count_date"]}　盘点人：{h["person"] or "—"}'
                            f'　监盘人：{h["supervisor"] or "—"}'
                            f'　实有合计（折 MOP）：'
                            f'{sum(i["actual"] * fx(i["currency"]) for i in items):,.2f}')
        tc.font = Font(name=FN, size=11, bold=True, color='1F4E79')
        tc.fill = PatternFill('solid', fgColor='DCE6F1')
        tc.alignment = Alignment(horizontal='left', vertical='center')
        ws3.row_dimensions[r].height = 22
        r += 1
        hd = ['币种'] + [f'{d:g}' for d in CASH_DENOMS] + ['张数合计', '金额合计']
        for c, v in enumerate(hd, 1):
            cell = ws3.cell(r, c, v)
            cell.font = Font(name=FN, size=10, bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='4472C4')
            cell.alignment = ctr
            cell.border = box
        r += 1
        for it in items:
            qmap = {d['denom']: d['qty'] for d in it['denoms']}
            ws3.cell(r, 1, it['currency']).alignment = ctr
            ws3.cell(r, 1).border = box
            ws3.cell(r, 1).font = base
            tot = 0.0
            nq = 0
            for c, dv in enumerate(CASH_DENOMS, 2):
                q = qmap.get(dv, 0) or 0
                tot += dv * q
                nq += q
                cell = ws3.cell(r, c, q if q else None)
                cell.alignment = ctr
                cell.border = box
                cell.font = base
                cell.number_format = '0.####'
            c1 = ws3.cell(r, len(CASH_DENOMS) + 2, nq or None)
            c1.alignment = ctr
            c1.border = box
            c1.font = base
            c2 = ws3.cell(r, len(CASH_DENOMS) + 3, round(it['actual'], 2))
            c2.number_format = money
            c2.alignment = right
            c2.border = box
            c2.font = bold
            r += 1
        # 无面额明细（只填了金额的老单）也给一行，金额仍有意义
        r += 1

    for w in wb.worksheets:
        w.page_setup.orientation = 'landscape'
        w.page_setup.fitToWidth = 1
        w.page_setup.fitToHeight = 0
        w.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        w.print_options.horizontalCentered = True

    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    span = f'{frm or "全部"}~{to or "全部"}' if (frm or to) else '全部'
    safe = re.sub(r'[\\/:*?"<>|]', '_', company)
    fname = f'{safe}_现金盘点表_{span}_{date.today():%Y%m%d}.xlsx'
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument'
                              '.spreadsheetml.sheet')



# ---------------------------------------------------------------- dashboard
@app.get('/api/dashboard')
def dashboard():
    cid = request.args.get('company_id', type=int)
    if not cid:
        return bad('缺少 company_id')
    db = get_db()
    accounts = []
    for a in db.execute('SELECT * FROM accounts WHERE company_id=? '
                        'ORDER BY sort, id', (cid,)):
        agg = db.execute(
            'SELECT COALESCE(SUM(debit),0) d, COALESCE(SUM(credit),0) c, '
            'COALESCE(SUM(CASE WHEN pending=1 THEN debit END),0) pd, '
            'COALESCE(SUM(CASE WHEN pending=1 THEN credit END),0) pc, '
            'COUNT(*) n FROM entries WHERE account_id=?',
            (a['id'],)).fetchone()
        book = a['opening'] + agg['d'] - agg['c']
        bank = book + agg['pc'] - agg['pd']
        accounts.append({**rowdict(a),
                         'book': book, 'bank': bank,
                         'pending_debit': agg['pd'], 'pending_credit': agg['pc'],
                         'entry_count': agg['n']})
    monthly = [rowdict(r) for r in db.execute(
        'SELECT substr(e.date,1,7) month, e.account_id, '
        'COALESCE(SUM(e.debit),0) debit, COALESCE(SUM(e.credit),0) credit '
        'FROM entries e JOIN accounts a ON a.id=e.account_id '
        'WHERE a.company_id=? GROUP BY month, e.account_id ORDER BY month', (cid,))]
    tag_counts = {r['tag_id']: r['c'] for r in db.execute(
        'SELECT et.tag_id, COUNT(*) c FROM entry_tags et '
        'JOIN entries e ON e.id=et.entry_id '
        'JOIN accounts a ON a.id=e.account_id '
        'WHERE a.company_id=? GROUP BY et.tag_id', (cid,))}
    deposits = [rowdict(r) for r in db.execute(
        'SELECT * FROM deposits WHERE company_id=? ORDER BY start_date, id', (cid,))]
    # 库存现金：按币种汇总 + 最近一次盘点
    cash_accs = [a for a in accounts if (a.get('kind') or 'bank') == 'cash']
    cash_by_cur = {}
    for a in cash_accs:
        o = cash_by_cur.setdefault(a['currency'], {'book': 0.0, 'n': 0})
        o['book'] += a['book']
        o['n'] += 1
    last_count = db.execute(
        'SELECT * FROM cash_counts WHERE company_id=? '
        'ORDER BY count_date DESC, id DESC LIMIT 1', (cid,)).fetchone()
    cash_last = None
    if last_count:
        pl = _cash_count_payload(db, last_count['id'])
        cash_last = {'count': pl['count'],
                     'items': [{k: v for k, v in i.items() if k != 'denoms'}
                               for i in pl['items']]}
    return jsonify({'accounts': accounts, 'monthly': monthly,
                    'tag_counts': tag_counts, 'deposits': deposits,
                    'cash': {'by_currency': cash_by_cur,
                             'account_count': len(cash_accs),
                             'last_count': cash_last}})


# ---------------------------------------------------------------- search
@app.get('/api/search')
def search():
    q = str(request.args.get('q') or '').strip()
    tag_id = request.args.get('tag_id', type=int)
    company_id = request.args.get('company_id', type=int)
    conds, params = [], []
    if q:
        like = f'%{q}%'
        conds.append('(e.description LIKE ? OR e.check_no LIKE ? OR '
                     'e.party LIKE ? OR e.project LIKE ?)')
        params += [like, like, like, like]
    if tag_id:
        conds.append('e.id IN (SELECT entry_id FROM entry_tags WHERE tag_id=?)')
        params.append(tag_id)
    if company_id:
        conds.append('a.company_id=?')
        params.append(company_id)
    if not conds:
        return bad('请输入搜索内容')
    sql = ('SELECT e.*, a.name account_name, a.currency, c.name company_name '
           'FROM entries e JOIN accounts a ON a.id=e.account_id '
           'JOIN companies c ON c.id=a.company_id WHERE '
           + ' AND '.join(conds) +
           ' ORDER BY e.date DESC, e.id DESC LIMIT 200')
    db = get_db()
    rows = db.execute(sql, params).fetchall()
    return jsonify(serialize_entries(db, rows))


# ---------------------------------------------------------------- export / backup
@app.get('/api/export/excel')
def export_excel():
    """?type=journal&account_id=N&from=YYYY-MM-DD&to=YYYY-MM-DD&tag_id=N&q=&group=1
       → 与界面一致的流水表：所选日期范围、已入账/未兑现分段、余额与合计均为公式
       ?type=deposits&company_id=N → 定期存款（天数/利息为公式）
       ?type=master&from=&to=&companies=1,2 → 全量导出：每个公司一个 Excel
                                 （目录页=公司信息概览 + 各账户余额跳转 + 每账户一页 + 定期存款）
                                 多公司时打包为 zip；默认本月、全部公司
                                 &sheets=4,5,8 只导出这些账户页（缺省＝全部）
                                 &dep=1,2 这些公司附带「定期存款」页（dep=0 表示都不带）
                                 &acc=4:起:止 给单个账户页单独指定期间
       无 type → 同 master"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties

    db = get_db()
    etype = request.args.get('type') or 'all'
    frm = (request.args.get('from') or '').strip()
    to = (request.args.get('to') or '').strip()
    if frm and not DATE_RE.match(frm):
        return bad('开始日期格式应为 YYYY-MM-DD')
    if to and not DATE_RE.match(to):
        return bad('结束日期格式应为 YYYY-MM-DD')
    tag_id = request.args.get('tag_id', type=int)
    q = (request.args.get('q') or '').strip()
    group = request.args.get('group') in ('1', 'true', 'yes')
    tag_name = ''
    if tag_id:
        tr = db.execute('SELECT name FROM tags WHERE id=?', (tag_id,)).fetchone()
        tag_name = tr['name'] if tr else ''

    # 每个工作表可单独指定日期：acc=4:2026-10-01:2026-10-31;5:2026-01-01:2026-12-31
    # 未出现在这里的账户沿用整体 from/to
    per_acc = {}
    for chunk in (request.args.get('acc') or '').split(';'):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(':')
        if len(parts) != 3 or not parts[0].strip().isdigit():
            continue
        _f, _t = parts[1].strip(), parts[2].strip()
        if (_f and not DATE_RE.match(_f)) or (_t and not DATE_RE.match(_t)):
            return bad('账户单独日期格式应为 YYYY-MM-DD')
        if _f or _t:
            per_acc[int(parts[0])] = (_f, _t)

    # 选择要导出哪些工作表：sheets=4,5,8（账户 id，决定每账户一页）
    #                       dep=1,2（这些公司额外导出「定期存款」页；0 表示都不导出）
    # 两者都不传＝全出（兼容旧调用）
    sel_sheets = None
    if request.args.get('sheets') is not None:
        sel_sheets = set()
        for chunk in (request.args.get('sheets') or '').split(';'):
            for x in chunk.split(','):
                x = x.strip()
                if x.isdigit():
                    sel_sheets.add(int(x))
    sel_dep = None
    if request.args.get('dep') is not None:
        sel_dep = {int(x) for x in (request.args.get('dep') or '').split(',')
                   if x.strip().isdigit()}

    wb = Workbook()
    thin = Side(style='thin', color='D9DEE7')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill('solid', fgColor='1F4E79')
    group_fill = PatternFill('solid', fgColor='EAF1F8')
    month_fill = PatternFill('solid', fgColor='F1F5F9')
    sum_fill = PatternFill('solid', fgColor='EDF2F9')
    pending_fill = PatternFill('solid', fgColor='FDEDEC')
    open_fill = PatternFill('solid', fgColor='F8FAFC')
    head_font = Font(bold=True, color='FFFFFF', size=12)
    title_font = Font(bold=True, size=16, color='1F4E79')
    sub_font = Font(size=11, color='64748B')
    base_font = Font(size=12)
    bold = Font(bold=True, size=12)
    bold_blue = Font(bold=True, size=12, color='1F4E79')
    gray_it = Font(color='808080', size=12)
    FONT_NAME = '微软雅黑'
    green_font = Font(color='0A7D5A', size=12)
    red_font = Font(color='C0392B', size=12)
    hint_font = Font(color='94A3B8', size=12)
    gray_font = Font(color='64748B', size=12)
    pending_font = Font(color='C0392B', size=12)
    money_fmt = '#,##0.00;[Red](#,##0.00)'
    ctr = Alignment(horizontal='center', vertical='center')
    left_wrap = Alignment(horizontal='left', vertical='center', wrap_text=True)
    right = Alignment(horizontal='right', vertical='center')
    ncol = 9

    def style_header(ws, row, cols):
        for c in range(1, cols + 1):
            cell = ws.cell(row, c)
            cell.font = head_font
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal='center', vertical='center')
            cell.border = border

    def block_row(ws, r, c1, c2, value=None, fill=None, font=None, align=None):
        if c2 > c1:
            ws.merge_cells(start_row=r, start_column=c1, end_row=r, end_column=c2)
        for c in range(c1, c2 + 1):
            cell = ws.cell(r, c)
            cell.border = border
            if fill is not None:
                cell.fill = fill
            if font is not None:
                cell.font = font
            if align is not None:
                cell.alignment = align
        if value is not None:
            ws.cell(r, c1, value)

    def fill_row(ws, r, c1, c2, fill):
        """只补底色和边框，不做合并 —— 用来把合并块与数值块之间的空列涂满，
        避免合计行中间出现白色缺口。"""
        for c in range(c1, c2 + 1):
            cell = ws.cell(r, c)
            cell.border = border
            cell.fill = fill

    def journal_accounts(where='', params=()):
        return db.execute(
            'SELECT a.*, c.name company_name FROM accounts a '
            'JOIN companies c ON c.id=a.company_id ' + where +
            ' ORDER BY c.sort, c.id, a.sort, a.id', params).fetchall()

    # ---------- 账页列定义（改列只需改这里） ----------
    C_DATE, C_NO, C_DESC, C_PARTY = 1, 2, 3, 4
    C_DEBIT, C_CREDIT, C_BAL, C_TAG, C_STATUS = 5, 6, 7, 8, 9
    L_DEBIT, L_CREDIT, L_BAL = 'E', 'F', 'G'

    # 账页默认期间（个别账户可覆盖）
    G_FROM, G_TO = frm, to

    def write_account_block(ws, r, acc, frm=G_FROM, to=G_TO):
        all_rows = db.execute('SELECT * FROM entries WHERE account_id=? '
                              'ORDER BY date, id', (acc['id'],)).fetchall()
        start_bal = acc['opening']
        if frm:
            start_bal += sum((e['debit'] - e['credit']) for e in all_rows
                             if e['date'] < frm and not e['pending'])

        # 真实实际余额：只算已入账，且包含被标签/关键词过滤掉的流水
        true_bal = {}
        run = start_bal
        for e in all_rows:
            if frm and e['date'] < frm:
                continue
            if to and e['date'] > to:
                break
            if not e['pending']:
                run += e['debit'] - e['credit']
            true_bal[e['id']] = run

        range_rows = [e for e in all_rows
                      if (not frm or e['date'] >= frm) and (not to or e['date'] <= to)]
        tag_all = entry_tags_map(db, [e['id'] for e in all_rows]) if tag_id else {}
        qq = q.lower()

        def visible(e):
            if tag_id and tag_id not in [t['id'] for t in tag_all.get(e['id'], [])]:
                return False
            if qq:
                hay = ''.join(str(e[k] or '') for k in
                              ('description', 'check_no', 'party', 'project')).lower()
                if qq not in hay:
                    return False
            return True

        rows = [e for e in range_rows if visible(e)]
        booked = [e for e in rows if not e['pending']]
        pend = [e for e in rows if e['pending']]
        all_booked = [e for e in range_rows if not e['pending']]
        end_true_all = true_bal.get(all_booked[-1]['id'], start_bal) if all_booked else start_bal
        tm = entry_tags_map(db, [e['id'] for e in rows])

        # 标题 / 筛选条件
        _is_cash = ('kind' in acc.keys() and acc['kind'] == 'cash')
        block_row(ws, r, 1, ncol,
                  f"{'现金日记账' if _is_cash else '日记账'} — "
                  f"{acc['company_name']} / {acc['name']}（{acc['currency']}）",
                  font=title_font, align=Alignment(horizontal='left', vertical='center'))
        ws.row_dimensions[r].height = 28
        r += 1
        info = (f"日期范围：{frm or '全部'} ~ {to or '全部'}　　"
                f"标签：{tag_name or '全部'}　　关键词：{q or '无'}　　"
                f"导出时间：{datetime.now():%Y-%m-%d %H:%M}")
        block_row(ws, r, 1, ncol, info, font=sub_font,
                  align=Alignment(horizontal='left', vertical='center'))
        ws.row_dimensions[r].height = 20
        r += 1

        # 期初实际余额（只有选了开始日期才显示，与界面一致）
        open_row = None
        if frm:
            hl_fill = PatternFill('solid', fgColor='BDD7EE')
            hl_label = Font(bold=True, size=12, color='1F4E79')
            hl_num = Font(bold=True, size=13, color='1F4E79')
            block_row(ws, r, 1, C_DESC, f"📌 期初实际余额（{frm} 前结转）",
                      fill=hl_fill, font=hl_label,
                      align=Alignment(horizontal='left', vertical='center'))
            for c in (C_PARTY, C_DEBIT, C_CREDIT):
                ws.cell(r, c).border = border
                ws.cell(r, c).fill = hl_fill
            cell = ws.cell(r, C_BAL, round(start_bal, 2))
            cell.number_format = money_fmt
            cell.font = hl_num
            cell.alignment = right
            cell.border = border
            cell.fill = hl_fill
            block_row(ws, r, C_TAG, C_STATUS, '', fill=hl_fill, font=hl_label)
            ws.row_dimensions[r].height = 32
            open_row = r
            r += 1

        headers = ['日期', '单据号', '摘要', '对方单位', '收入（借方）', '支出（贷方）',
                   '实际余额', '标签 / 备注', '状态']
        for c, h in enumerate(headers, 1):
            ws.cell(r, c, h)
        style_header(ws, r, ncol)
        ws.row_dimensions[r].height = 24
        header_row = r
        r += 1

        def write_entry(rr, e, status, bal_formula=None, bal_value=None, is_pending=False):
            for c in range(1, ncol + 1):
                cell = ws.cell(rr, c)
                cell.font = base_font
                cell.border = border
            dc = ws.cell(rr, C_DATE, datetime.strptime(e['date'], '%Y-%m-%d').date())
            dc.number_format = 'yyyy-mm-dd'
            dc.alignment = ctr
            ws.cell(rr, C_NO, e['check_no'] or None).alignment = ctr
            desc = e['description'] or ''
            if e['project']:
                desc += ('\n' if desc else '') + '项目：' + e['project']
            ws.cell(rr, C_DESC, desc or None).alignment = left_wrap
            ws.cell(rr, C_PARTY, e['party'] or None).alignment = left_wrap
            if e['debit']:
                c4 = ws.cell(rr, C_DEBIT, e['debit'])
                c4.number_format = money_fmt
                c4.font = green_font
                c4.alignment = right
            if e['credit']:
                c5 = ws.cell(rr, C_CREDIT, e['credit'])
                c5.number_format = money_fmt
                c5.font = red_font
                c5.alignment = right
            fc = ws.cell(rr, C_BAL)
            if bal_formula is not None:
                fc.value = bal_formula
                fc.number_format = money_fmt
                fc.alignment = right
            elif bal_value is not None:
                fc.value = bal_value
                fc.number_format = money_fmt
                fc.alignment = right
            else:
                fc.value = '—'
                fc.alignment = ctr
                fc.font = hint_font
            tags = '、'.join(t['name'] for t in tm.get(e['id'], []))
            if e['note']:
                tags += ('\n' if tags else '') + '📝 ' + e['note']
            ws.cell(rr, C_TAG, tags or None).alignment = left_wrap
            ws.cell(rr, C_STATUS, status).alignment = ctr
            # 摘要/标签折行时按估算行数加高行，避免文字被裁掉
            def _lines(text, col_w):
                n = 0
                cap = max(1, int(col_w * 0.88))
                for seg in str(text or '').split('\n'):
                    need = sum(2 if ord(ch) > 127 else 1 for ch in seg)
                    n += max(1, -(-need // cap))
                return n
            n_line = max(_lines(desc, 46), _lines(e['party'], 24), _lines(tags, 26))
            if n_line > 1:
                ws.row_dimensions[rr].height = min(17.5 * n_line, 88)
            if is_pending:
                for c in (C_DATE, C_NO, C_DESC, C_PARTY, C_TAG, C_STATUS):
                    ws.cell(rr, c).font = pending_font
                if fc.value == '—':
                    fc.font = Font(color='E28B84', size=12)

        # 已入账
        block_row(ws, r, 1, ncol,
                  f"✅ 已入账（{len(booked)} 笔）　余额＝实际余额（不含未兑现/未到账）",
                  fill=group_fill, font=Font(bold=True, color='1F4E79', size=12),
                  align=left_wrap)
        ws.row_dimensions[r].height = 22
        r += 1

        prev_ref = f'${L_BAL}${open_row}' if open_row else None
        prev_true = start_bal
        booked_rows_written = []
        month_rows = []
        can_group = len({e['date'][:7] for e in range_rows}) > 1
        group_mode = group and can_group and booked

        def emit_booked(e):
            nonlocal r, prev_ref, prev_true
            cur_true = true_bal[e['id']]
            adj = round(cur_true - prev_true - (e['debit'] - e['credit']), 2)
            base = prev_ref if prev_ref else f'{round(start_bal, 2)}'
            formula = (f'={base}+{adj}+{L_DEBIT}{r}-{L_CREDIT}{r}' if abs(adj) > 0.004
                       else f'={base}+{L_DEBIT}{r}-{L_CREDIT}{r}')
            write_entry(r, e, '已入账', bal_formula=formula)
            booked_rows_written.append(r)
            prev_ref = f'${L_BAL}${r}'
            prev_true = cur_true
            r += 1

        if group_mode:
            months = []
            for e in booked:
                mo = e['date'][:7]
                if not months or months[-1][0] != mo:
                    months.append([mo, []])
                months[-1][1].append(e)
            for mo, lst in months:
                block_row(ws, r, 1, C_DESC, f"📅 {mo}　共 {len(lst)} 笔",
                          fill=month_fill, font=Font(bold=True, color='475569', size=12),
                          align=left_wrap)
                fill_row(ws, r, C_PARTY, ncol, month_fill)
                for c, col in ((C_DEBIT, L_DEBIT), (C_CREDIT, L_CREDIT)):
                    cell = ws.cell(r, c, f'=SUM({col}{r+1}:{col}{r+len(lst)})')
                    cell.number_format = money_fmt
                    cell.font = bold
                    cell.alignment = right
                    cell.border = border
                    cell.fill = month_fill
                fcell = ws.cell(r, C_BAL, f'={L_BAL}{r+len(lst)}')
                fcell.number_format = money_fmt
                fcell.font = bold
                fcell.alignment = right
                fcell.border = border
                fcell.fill = month_fill
                block_row(ws, r, C_TAG, C_STATUS, '月末实际余额', fill=month_fill,
                          font=Font(bold=True, color='64748B', size=12), align=left_wrap)
                ws.row_dimensions[r].height = 22
                month_rows.append(r)
                r += 1
                for e in lst:
                    emit_booked(e)
        else:
            for e in booked:
                emit_booked(e)

        if not booked:
            block_row(ws, r, 1, ncol,
                      '当前筛选下没有已入账流水' if rows else '暂无流水',
                      font=hint_font, align=left_wrap)
            ws.row_dimensions[r].height = 22
            r += 1

        # 已入账合计
        if month_rows:
            dsum = '=SUM(' + ','.join(f'{L_DEBIT}{x}' for x in month_rows) + ')'
            esum = '=SUM(' + ','.join(f'{L_CREDIT}{x}' for x in month_rows) + ')'
        elif booked_rows_written:
            dsum = f'=SUM({L_DEBIT}{booked_rows_written[0]}:{L_DEBIT}{booked_rows_written[-1]})'
            esum = f'=SUM({L_CREDIT}{booked_rows_written[0]}:{L_CREDIT}{booked_rows_written[-1]})'
        else:
            dsum = esum = None

        # 合计余额始终等于日期范围末的真实实际余额
        if booked_rows_written:
            diff = round(end_true_all - prev_true, 2)
            ftotal = (f'={L_BAL}{booked_rows_written[-1]}+{diff}' if abs(diff) > 0.004
                      else f'={L_BAL}{booked_rows_written[-1]}')
        elif open_row:
            diff = round(end_true_all - start_bal, 2)
            ftotal = (f'={L_BAL}{open_row}+{diff}' if abs(diff) > 0.004
                      else f'={L_BAL}{open_row}')
        else:
            ftotal = round(end_true_all, 2)

        filter_note = '；已按标签/关键词筛选，实际余额含未显示流水' if (tag_id or qq) else ''
        block_row(ws, r, 1, C_DESC,
                  f"已入账合计　共 {len(booked)} 笔（当前筛选）{filter_note}",
                  fill=sum_fill, font=bold_blue, align=left_wrap)
        fill_row(ws, r, C_PARTY, ncol, sum_fill)
        for c, fml, color in ((C_DEBIT, dsum, '0A7D5A'), (C_CREDIT, esum, 'C0392B')):
            cell = ws.cell(r, c, fml if fml else 0)
            cell.number_format = money_fmt
            cell.font = Font(bold=True, color=color, size=12)
            cell.alignment = right
            cell.border = border
            cell.fill = sum_fill
        fcell = ws.cell(r, C_BAL, ftotal)
        fcell.number_format = money_fmt
        fcell.font = bold_blue
        fcell.alignment = right
        fcell.border = border
        fcell.fill = sum_fill
        block_row(ws, r, C_TAG, C_STATUS, '实际余额', fill=sum_fill, font=gray_font,
                  align=left_wrap)
        ws.row_dimensions[r].height = 30
        total_row = r
        r += 1

        # 未兑现 / 未到账
        block_row(ws, r, 1, ncol,
                  f"未兑现 / 未到账（{len(pend)} 笔）　收入增加余额、支出减少余额",
                  fill=pending_fill, font=Font(bold=True, color='B42318', size=12),
                  align=left_wrap)
        ws.row_dimensions[r].height = 22
        r += 1
        p_first = r
        for e in pend:
            write_entry(r, e, '未兑现/未到账', is_pending=True)
            r += 1
        p_last = r - 1
        if not pend:
            block_row(ws, r, 1, ncol, '没有未兑现/未到账的款项',
                      font=hint_font, align=left_wrap)
            r += 1

        end_label = to or (range_rows[-1]['date'] if range_rows else '')

        # 第一行：未兑现 / 未到账的收支合计（只有收入、支出两列）
        block_row(ws, r, 1, C_DESC, f"未兑现 / 未到账合计（{len(pend)} 笔）",
                  fill=pending_fill,
                  font=Font(bold=True, color='B42318', size=12), align=left_wrap)
        fill_row(ws, r, C_PARTY, ncol, pending_fill)
        if pend:
            dc = ws.cell(r, C_DEBIT, f'=SUM({L_DEBIT}{p_first}:{L_DEBIT}{p_last})')
            ec = ws.cell(r, C_CREDIT, f'=SUM({L_CREDIT}{p_first}:{L_CREDIT}{p_last})')
        else:
            dc = ws.cell(r, C_DEBIT, 0)
            ec = ws.cell(r, C_CREDIT, 0)
        for c, cell in ((C_DEBIT, dc), (C_CREDIT, ec)):
            cell.number_format = money_fmt
            cell.font = Font(bold=True, color='0A7D5A' if c == C_DEBIT else 'C0392B', size=12)
            cell.alignment = right
            cell.border = border
            cell.fill = pending_fill
        block_row(ws, r, C_BAL, C_STATUS, f"截至 {end_label or '—'}", fill=pending_fill,
                  font=Font(bold=True, color='B42318', size=12), align=left_wrap)
        ws.row_dimensions[r].height = 24
        pend_total_row = r
        r += 1

        # 第二行：计入未兑现 / 未到账后的实际余额
        block_row(ws, r, 1, C_CREDIT, '计入未兑现 / 未到账后的实际余额',
                  fill=sum_fill, font=bold_blue, align=left_wrap)
        fcell = ws.cell(r, C_BAL,
                        f'={L_BAL}{total_row}+{L_DEBIT}{pend_total_row}-{L_CREDIT}{pend_total_row}')
        fcell.number_format = money_fmt
        fcell.font = bold_blue
        fcell.alignment = right
        fcell.border = border
        fcell.fill = sum_fill
        block_row(ws, r, C_TAG, C_STATUS, '已入账余额 + 未兑现收入 - 未兑现支出', fill=sum_fill,
                  font=gray_font, align=left_wrap)
        ws.row_dimensions[r].height = 30
        r += 1
        return r, header_row

    def finish_journal_ws(ws, first_header):
        for i, w in enumerate([13, 20, 46, 24, 17, 17, 17, 26, 15], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        if first_header:
            ws.freeze_panes = f'A{first_header + 1}'
        ws.page_setup.orientation = 'landscape'
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)

    def write_journal_sheet(accs):
        ws = wb.active
        ws.title = '银行流水'
        r = 1
        first_header = None
        for acc in accs:
            r, hdr = write_account_block(ws, r, acc)
            if first_header is None:
                first_header = hdr
            r += 2
        finish_journal_ws(ws, first_header)

    def to_date(s):
        try:
            return datetime.strptime(s, '%Y-%m-%d').date() if s else None
        except (ValueError, TypeError):
            return None

    def write_deposits_sheet(company_id=None):
        if wb.active.title == 'Sheet' and wb.active.max_row == 1:
            ws2 = wb.active
            ws2.title = '定期存款'
        else:
            ws2 = wb.create_sheet('定期存款')
        h2 = ['公司', '银行', '定单号', '币别', '本金', '定存期', '年利率',
              '开始日期', '到期日', '总天数', '到期利息', '状态', '备注']
        ws2.append(h2)
        style_header(ws2, 1, len(h2))
        ws2.row_dimensions[1].height = 24
        today = date.today()
        if company_id:
            dep_rows = db.execute(
                'SELECT d.*, c.name company_name FROM deposits d '
                'JOIN companies c ON c.id=d.company_id WHERE d.company_id=? '
                'ORDER BY d.start_date, d.id', (company_id,)).fetchall()
        else:
            dep_rows = db.execute(
                'SELECT d.*, c.name company_name FROM deposits d '
                'JOIN companies c ON c.id=d.company_id ORDER BY c.sort, d.start_date').fetchall()
        for i, d in enumerate(dep_rows, start=2):
            sd, ed = to_date(d['start_date']), to_date(d['end_date'])
            if not ed:
                status = '未设到期日'
            else:
                left = (ed - today).days
                status = (f'已到期 {-left} 天' if left < 0 else
                          '今天到期' if left == 0 else
                          f'{left} 天后到期' if left <= 30 else '存续中')
            vals = [d['company_name'], d['bank'], d['order_no'], d['currency'],
                    d['principal'], d['period'], d['rate'], sd, ed, None, None,
                    status, d['note']]
            for c, v in enumerate(vals, 1):
                cell = ws2.cell(i, c, v)
                cell.border = border
                cell.font = base_font
                cell.alignment = left_wrap if c in (1, 2, 3, 13) else ctr
                if c == 5:
                    cell.number_format = '#,##0.00'
                if c == 7:
                    cell.number_format = '0.00%'
                if c in (8, 9):
                    cell.number_format = 'yyyy-mm-dd'
            if sd and ed:
                dc = ws2.cell(i, 10, f'=I{i}-H{i}')
                dc.number_format = '0'
                dc.font = base_font
                dc.alignment = ctr
                ic = ws2.cell(i, 11, f'=ROUND(E{i}*G{i}*J{i}/365,2)')
                ic.number_format = '#,##0.00'
                ic.font = base_font
                ic.alignment = right
        for i, w in enumerate([24, 18, 22, 7, 17, 9, 9, 13, 13, 9, 15, 15, 26], 1):
            ws2.column_dimensions[get_column_letter(i)].width = w
        ws2.page_setup.orientation = 'landscape'
        ws2.page_setup.fitToWidth = 1
        ws2.page_setup.fitToHeight = 0
        ws2.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)

    # ---------------- 现金盘点工作表（主表 + 面额明细块）
    def write_cash_counts_sheet(company_id):
        if wb.active.title == 'Sheet' and wb.active.max_row == 1:
            ws3 = wb.active
            ws3.title = '现金盘点'
        else:
            ws3 = wb.create_sheet('现金盘点')
        heads = ['盘点日期', '币种', '上一日账面库存', '未记账收入', '未记账付出',
                 '盘点日账面应有', '盘点实有现金', '差异', '白条抵库',
                 '差异原因', '盘点人', '监盘人']
        ws3.append(heads)
        style_header(ws3, 1, len(heads))
        ws3.row_dimensions[1].height = 26
        money_cols = (3, 4, 5, 6, 7, 8, 9)
        r = 2
        counts = db.execute('SELECT * FROM cash_counts WHERE company_id=? '
                            'ORDER BY count_date, id', (company_id,)).fetchall()
        for h in counts:
            items = []
            for it in db.execute('SELECT * FROM cash_count_items WHERE count_id=? '
                                 'ORDER BY id', (h['id'],)):
                ds = _denoms_of(db, it['id'])
                items.append({**rowdict(it), **_cash_item_calc(rowdict(it), ds)})
            if not items:
                continue
            for it in items:
                vals = [h['count_date'], it['currency'], it['prev_book'],
                        it['unposted_in'], it['unposted_out'], it['book'],
                        it['actual'], it['diff'], it['white'],
                        it['reason'], h['person'], h['supervisor']]
                for c, v in enumerate(vals, 1):
                    cell = ws3.cell(r, c, v)
                    cell.border = border
                    cell.font = base_font
                    cell.alignment = left_wrap if c == 10 else ctr
                    if c in money_cols:
                        cell.number_format = '#,##0.00'
                        cell.alignment = right
                    if c == 8 and abs(it['diff']) > 0.005:
                        cell.font = Font(bold=True, size=11, color='C0392B')
                r += 1
        # ---------- 面额明细块
        if counts:
            r += 1
            for h in counts:
                items = []
                for it in db.execute('SELECT * FROM cash_count_items WHERE count_id=? '
                                     'ORDER BY id', (h['id'],)):
                    ds = _denoms_of(db, it['id'])
                    if ds:
                        items.append((rowdict(it), {d['denom']: d['qty'] for d in ds}))
                if not items:
                    continue
                title = (f'▍面额明细　{h["count_date"]}'
                         f'　盘点人：{h["person"] or "—"}'
                         f'　监盘人：{h["supervisor"] or "—"}')
                ws3.merge_cells(start_row=r, start_column=1,
                                end_row=r, end_column=len(CASH_DENOMS) + 2)
                tc = ws3.cell(r, 1, title)
                tc.font = Font(bold=True, size=12, color='1F4E79')
                tc.fill = PatternFill('solid', fgColor='DCE6F1')
                tc.alignment = Alignment(horizontal='left', vertical='center')
                ws3.row_dimensions[r].height = 22
                r += 1
                hd = ['币种'] + [f'{d:g}' for d in CASH_DENOMS] + ['合计金额']
                for c, v in enumerate(hd, 1):
                    cell = ws3.cell(r, c, v)
                    cell.font = Font(bold=True, size=10, color='FFFFFF')
                    cell.fill = PatternFill('solid', fgColor='4472C4')
                    cell.alignment = ctr
                    cell.border = border
                r += 1
                for it, qtys in items:
                    total = 0.0
                    ws3.cell(r, 1, it['currency']).alignment = ctr
                    ws3.cell(r, 1).border = border
                    ws3.cell(r, 1).font = base_font
                    for c, dv in enumerate(CASH_DENOMS, 2):
                        q = qtys.get(dv, 0) or 0
                        total += dv * q
                        cell = ws3.cell(r, c, q if q else None)
                        cell.alignment = ctr
                        cell.border = border
                        cell.font = base_font
                        cell.number_format = '0.####'
                    cell = ws3.cell(r, len(CASH_DENOMS) + 2, round(total, 2))
                    cell.number_format = '#,##0.00'
                    cell.alignment = right
                    cell.border = border
                    cell.font = Font(bold=True, size=11)
                    r += 1
                r += 1
        for i, w in enumerate([13, 8, 16, 13, 13, 15, 15, 13, 12, 32, 12, 12], 1):
            ws3.column_dimensions[get_column_letter(i)].width = w
        ws3.page_setup.orientation = 'landscape'
        ws3.page_setup.fitToWidth = 1
        ws3.page_setup.fitToHeight = 0
        ws3.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)

    # ---------------- 全量导出：按公司生成工作簿（目录 + 每个账户一页 + 定期存款） ----------------
    def safe_sheet_name(base, used):
        name = re.sub(r'[\[\]:*?/\\]', '_', str(base)).strip() or 'Sheet'
        name = name[:31]
        if name in used:
            i = 2
            while True:
                suffix = f'（{i}）'
                cand = name[:31 - len(suffix)] + suffix
                if cand not in used:
                    name = cand
                    break
                i += 1
        used.add(name)
        return name

    def build_company_book(cp, all_accs, deps, rates_map):
        """为一家公司生成一个独立工作簿：目录（含各账户余额与跳转）+ 每账户一页 + 定期存款"""
        nonlocal wb
        wb = Workbook()
        ws = wb.active
        ws.title = '目录'
        ws.sheet_view.showGridLines = False
        NC = 11
        used = {'目录', '定期存款', '现金盘点'}

        accs = [a for a in all_accs if a['company_id'] == cp['id']]
        my_deps = [d for d in deps if d['company_id'] == cp['id']]
        all_my_accs = accs
        if sel_sheets is not None:
            accs = [a for a in accs if a['id'] in sel_sheets]
        incl_dep = bool(my_deps) and (sel_dep is None or cp['id'] in sel_dep)
        if not accs and not incl_dep:
            return None            # 这家公司一个工作表都没选，直接跳过
        fx = lambda cur: (rates_map.get(cur, 1) or 1)

        def acc_rng(a):
            """该账户自己的工作表期间：单独指定过就用它，否则跟随整体"""
            return per_acc.get(a['id'], (frm, to))

        def acc_range_stats(acc, frm, to):
            """与单账户工作表完全一致的区间口径"""
            rows = db.execute('SELECT date, debit, credit, pending FROM entries '
                              'WHERE account_id=? ORDER BY date, id',
                              (acc['id'],)).fetchall()
            start = acc['opening']
            for e in rows:
                if frm and e['date'] < frm and not e['pending']:
                    start += e['debit'] - e['credit']
            r_in = r_out = p_in = p_out = 0.0
            n = 0
            for e in rows:
                if (frm and e['date'] < frm) or (to and e['date'] > to):
                    continue
                n += 1
                if e['pending']:
                    p_in += e['debit']
                    p_out += e['credit']
                else:
                    r_in += e['debit']
                    r_out += e['credit']
            end_true = start + r_in - r_out
            return {'start': start, 'in': r_in, 'out': r_out,
                    'pend_in': p_in, 'pend_out': p_out,
                    'end_true': end_true, 'end_book': end_true + p_out - p_in,
                    'n': n}

        stats = {a['id']: acc_range_stats(a, *acc_rng(a)) for a in accs}
        # 「定期户」＝名称含「定期」的流水账户：它的余额本身就是存单本金，
        # 若再把存单本金叠加一次就会重复计算资产。有定期户时以定期户余额为准。
        dep_accs = [a for a in accs if '定期' in (a['name'] or '')]
        dep_ids = {a['id'] for a in dep_accs}
        # 库存现金账户（kind='cash'）单独统计，与银行往来分开
        cash_kind_accs = [a for a in accs
                          if (a['kind'] or 'bank') == 'cash'
                          and a['id'] not in dep_ids]
        cash_kind_ids = {a['id'] for a in cash_kind_accs}
        bank_accs = [a for a in accs
                     if a['id'] not in dep_ids and a['id'] not in cash_kind_ids]
        bank_mop = sum(stats[a['id']]['end_true'] * fx(a['currency']) for a in bank_accs)
        cash_kind_mop = sum(stats[a['id']]['end_true'] * fx(a['currency'])
                            for a in cash_kind_accs)
        dep_acc_mop = sum(stats[a['id']]['end_true'] * fx(a['currency']) for a in dep_accs)
        dep_mop = sum(d['principal'] * fx(d['currency']) for d in my_deps)
        # 计入资产的定期部分：有定期户 → 定期户余额（存单已并入）；
        # 没导出定期户时，只有「定期存款」页也一起导出才计入存单本金
        dep_asset_mop = (dep_acc_mop if dep_accs
                         else (dep_mop if incl_dep else 0.0))
        asset_mop = bank_mop + dep_asset_mop + cash_kind_mop
        # 定期相关内容是否属于本次导出范围（否则目录里不显示，免得和合计对不上）
        show_dep = bool(my_deps) and (bool(dep_accs) or incl_dep)
        n_flow = sum(stats[a['id']]['n'] for a in accs)

        def merge_text(rr, c1, c2, value, font=None, fill=None, align=None):
            if c2 > c1:
                ws.merge_cells(start_row=rr, start_column=c1,
                               end_row=rr, end_column=c2)
            cell = ws.cell(rr, c1, value)
            if font:
                cell.font = font
            if align:
                cell.alignment = align
            if fill is not None:
                for c in range(c1, c2 + 1):
                    ws.cell(rr, c).fill = fill

        def section(rr, text, span=None, fill=group_fill, color='1F4E79'):
            span = span or NC
            merge_text(rr, 1, span, text, fill=fill,
                       font=Font(bold=True, size=13, color=color),
                       align=Alignment(horizontal='left', vertical='center'))
            for c in range(span + 1, NC + 1):
                ws.cell(rr, c).fill = fill
            ws.row_dimensions[rr].height = 26
            return rr + 1

        def table_header(rr, groups):
            for c1, c2, label in groups:
                if c2 > c1:
                    ws.merge_cells(start_row=rr, start_column=c1,
                                   end_row=rr, end_column=c2)
                ws.cell(rr, c1, label)
            for c in range(1, NC + 1):
                cell = ws.cell(rr, c)
                cell.font = head_font
                cell.fill = head_fill
                cell.alignment = Alignment(horizontal='center',
                                           vertical='center', wrap_text=True)
                cell.border = border
            ws.row_dimensions[rr].height = 26
            return rr + 1

        def data_row(rr, cells, fill=None, font=None, height=22):
            """cells: [(c1, c2, value, kind)]，kind: text/money/int/date/center/link"""
            for c1, c2, val, kind in cells:
                if c2 > c1:
                    ws.merge_cells(start_row=rr, start_column=c1,
                                   end_row=rr, end_column=c2)
                cell = ws.cell(rr, c1, val)
                if kind == 'money':
                    cell.number_format = money_fmt
                    cell.alignment = right
                elif kind == 'int':
                    cell.number_format = '0'
                    cell.alignment = ctr
                elif kind == 'date':
                    cell.number_format = 'yyyy-mm-dd'
                    cell.alignment = ctr
                elif kind == 'center':
                    cell.alignment = ctr
                else:
                    cell.alignment = left_wrap
            for c in range(1, NC + 1):
                cell = ws.cell(rr, c)
                cell.border = border
                if fill is not None:
                    cell.fill = fill
                if font is not None:
                    cell.font = font
            ws.row_dimensions[rr].height = height
            return rr + 1

        link_font = Font(color='0563C1', size=12, underline='single')
        from openpyxl.worksheet.hyperlink import Hyperlink

        def set_internal_link(cell, sheet_name, anchor='A1'):
            """工作表内部跳转：用 location 而不是外部 rels，Excel / WPS 都认"""
            cell.hyperlink = Hyperlink(ref=cell.coordinate,
                                       location=f"'{sheet_name}'!{anchor}",
                                       display=str(cell.value or ''))
            cell.font = link_font

        # 先分配账户 sheet 名（目录里要写超链接）
        names = [safe_sheet_name(f'{i:02d} {a["name"]}', used)
                 for i, a in enumerate(accs, 1)]

        # ---------- 抬头
        r = 1
        merge_text(r, 1, NC, f'{cp["name"]}　日记账',
                   font=Font(bold=True, size=18, color='1F4E79'),
                   align=Alignment(horizontal='left', vertical='center'))
        ws.row_dimensions[r].height = 34
        r += 1
        # 有账户单独设了期间时，抬头要说明（各页期间见账户余额目录的「期间」列）
        custom_rng = [a for a in accs if acc_rng(a) != (frm, to)]
        rng_note = ''
        if custom_rng:
            rng_note = (f'　　※ 其中 {len(custom_rng)} 个账户单独设定期间，'
                        f'详见「账户余额目录」的期间列')
        merge_text(r, 1, NC,
                   f'日期范围：{frm or "全部"} ~ {to or "全部"}{rng_note}　　'
                   f'生成时间：{datetime.now():%Y-%m-%d %H:%M}　　'
                   f'币种折算：按「设置 → 汇率」折 MOP　　'
                   f'余额口径：银行实际余额（不含未兑现支票 / 未到账收入）',
                   font=sub_font, align=Alignment(horizontal='left', vertical='center'))
        ws.row_dimensions[r].height = 20 if not custom_rng else 30
        ws.cell(r, 1).alignment = Alignment(horizontal='left', vertical='center',
                                            wrap_text=True)
        r += 2

        # ---------- 公司信息概览
        r = section(r, '🏢 公司信息概览', span=3)
        cur_cnt = {}
        for a in accs:
            cur_cnt[a['currency']] = cur_cnt.get(a['currency'], 0) + 1
        cur_txt = ' / '.join(f'{k} {v}' for k, v in sorted(cur_cnt.items()))

        def bal_text(acc_list):
            d = {}
            for a in acc_list:
                d[a['currency']] = d.get(a['currency'], 0) + stats[a['id']]['end_true']
            return '　'.join(f'{k} {v:,.2f}' for k, v in sorted(d.items()))

        cash_txt = bal_text(bank_accs)
        cash_kind_txt = bal_text(cash_kind_accs)
        dep_acc_txt = bal_text(dep_accs)
        dates = sorted(d['end_date'] for d in my_deps
                       if DATE_RE.match(d['end_date'] or ''))
        if not accs:
            acc_val = (f'0 个（本次未导出账户页，全公司共 {len(all_my_accs)} 个账户）'
                       if all_my_accs else '0 个')
        else:
            acc_val = (f'{len(accs)} 个' + (f'（按币种：{cur_txt}）' if cur_txt else '')
                       + (f'　※ 全公司共 {len(all_my_accs)} 个账户，本次只导出 {len(accs)} 个，'
                          f'下列金额只含已导出部分'
                          if len(accs) < len(all_my_accs) else ''))
        info_rows = [
            ('公司名称', cp['name']),
            ('账户数', acc_val),
            ('本期流水', f'{n_flow} 笔（{frm or "开始"} ~ {to or "至今"}）'),
            ('银行余额（活期/往来）',
             f'折 MOP {bank_mop:,.2f}' + (f'　｜　{cash_txt}' if cash_txt else '')),
        ]
        if cash_kind_accs:
            info_rows.append(
                ('库存现金',
                 f'折 MOP {cash_kind_mop:,.2f}'
                 + (f'　｜　{cash_kind_txt}' if cash_kind_txt else '')
                 + '　（现金日记账，未扣白条抵库）'))
        if dep_accs:
            info_rows.append(
                ('定期账户余额',
                 f'折 MOP {dep_acc_mop:,.2f}'
                 + (f'　｜　{dep_acc_txt}' if dep_acc_txt else '')
                 + f'　（{"、".join(a["name"] for a in dep_accs)}）'))
        if show_dep:
            info_rows.append(
                ('定期存款（存单）',
                 f'{len(my_deps)} 笔　本金折 MOP {dep_mop:,.2f}'
                 + (f'　最近到期 {dates[0]}' if dates else '')
                 + ('　※ 已含在上述定期户余额内' if dep_accs else '')))
        asset_tail = (' + '.join(x for x in [
            '银行余额',
            '定期账户余额' if dep_accs else ('定期本金' if incl_dep else ''),
            '库存现金' if cash_kind_accs else '',
        ] if x) or '仅银行余额')
        if not (dep_accs or incl_dep or cash_kind_accs):
            asset_tail = '仅银行余额，本次未导出定期/现金相关内容'
        info_rows.append(
            ('资产合计', f'折 MOP {asset_mop:,.2f}（{asset_tail}）'))
        for label, value in info_rows:
            merge_text(r, 1, 3, label, fill=open_fill,
                       font=Font(bold=True, size=12, color='475569'),
                       align=Alignment(horizontal='left', vertical='center'))
            merge_text(r, 4, NC, value,
                       font=Font(size=12, color='0F172A'),
                       align=Alignment(horizontal='left', vertical='center',
                                       wrap_text=True))
            for c in range(1, NC + 1):
                ws.cell(r, c).border = border
            ws.row_dimensions[r].height = 24
            r += 1
        r += 1

        # ---------- 账户余额目录（带超链接，余额加粗突出）
        r = section(r, '📇 账户余额目录　（点账户名称或「打开明细」跳到该账户的工作表）')
        r = table_header(r, [
            (1, 1, '序号'), (2, 2, '账户名称'), (3, 3, '币别'), (4, 4, '银行'),
            (5, 5, '账号'), (6, 6, '期末余额'), (7, 7, '未兑现支票'),
            (8, 8, '未到账收入'), (9, 9, '流水笔数'), (10, 10, '期间'),
            (11, 11, '明细'),
        ])
        bal_fill = PatternFill('solid', fgColor='EAF1F8')
        bal_font = Font(bold=True, size=13, color='1F4E79')
        custom_font = Font(bold=True, size=11, color='B45309')

        def rng_label(a):
            """目录里的期间简写：同年只写月-日，跨年写全年份"""
            f, t = acc_rng(a)
            if not f and not t:
                return '全部'
            if not f:
                return f'～{t}'
            if not t:
                return f'{f}～'
            if f[:4] == t[:4]:
                return f'{f[5:]}～{t[5:]}'
            return f'{f}～{t}'

        for i, a in enumerate(accs, 1):
            s = stats[a['id']]
            rr = r
            is_custom = acc_rng(a) != (frm, to)
            is_cash = a['id'] in cash_kind_ids
            r = data_row(r, [
                (1, 1, i, 'int'),
                (2, 2, ('💵 ' if is_cash else '') + a['name'], 'link'),
                (3, 3, a['currency'], 'center'),
                (4, 4, a['bank'] or ('现金库' if is_cash else ''), 'text'),
                (5, 5, a['account_no'] or '', 'text'),
                (6, 6, round(s['end_true'], 2), 'money'),
                (7, 7, round(s['pend_out'], 2), 'money'),
                (8, 8, round(s['pend_in'], 2), 'money'),
                (9, 9, s['n'], 'int'),
                (10, 10, rng_label(a), 'center'),
                (11, 11, '打开明细 →', 'link'),
            ], height=24)
            pc = ws.cell(rr, 10)
            if is_custom:
                pc.font = custom_font
                pc.fill = PatternFill('solid', fgColor='FEF3C7')
            for c in (2, 11):
                set_internal_link(ws.cell(rr, c), names[i - 1])
            bc = ws.cell(rr, 6)
            bc.font = bal_font
            bc.fill = bal_fill
        if not accs:
            block_row(ws, r, 1, NC,
                      '本次未勾选任何账户页（只导出了定期存款页）' if all_my_accs
                      else '该公司还没有账户', font=hint_font,
                      align=left_wrap)
            r += 1
        r += 1

        # ---------- 定期存款汇总（只在定期内容属于本次导出范围时出现）
        if show_dep:
            r = section(r, '💰 定期存款　（按存单到期日，不受日期范围筛选）', span=5)
            lk = ws.cell(r - 1, 6, '→ 定期明细' if incl_dep else '（未导出明细）')
            lk.alignment = Alignment(horizontal='left', vertical='center')
            if incl_dep:
                set_internal_link(lk, '定期存款')
            else:
                lk.font = Font(size=11, color='94A3B8')
            r = table_header(r, [(1, 2, '笔数'), (3, 4, '本金（折MOP）'),
                                 (5, 5, '最近到期日'), (6, 6, '已到期'),
                                 (7, 7, '30天内到期'), (8, NC, '备注')])
            today = date.today()
            due = soon = 0
            for ds in dates:
                left = (datetime.strptime(ds, '%Y-%m-%d').date() - today).days
                if left < 0:
                    due += 1
                elif left <= 30:
                    soon += 1
            r = data_row(r, [
                (1, 2, len(my_deps), 'int'),
                (3, 4, round(dep_mop, 2), 'money'),
                (5, 5, dates[0] if dates else '—', 'date'),
                (6, 6, due, 'int'),
                (7, 7, soon, 'int'),
                (8, NC, '已含在定期户余额内，不重复计入' if dep_accs
                 else '本金为折 MOP，明细见「定期存款」工作表', 'text'),
            ])
            r += 1

        # ---------- 现金盘点（有库存现金账户时才出现）
        incl_cash = bool(cash_kind_accs)
        if incl_cash:
            counts = db.execute(
                'SELECT * FROM cash_counts WHERE company_id=? '
                'ORDER BY count_date DESC, id DESC', (cp['id'],)).fetchall()
            r = section(r, '💵 现金盘点　（库存现金对账记录，不受日期范围筛选）', span=6)
            lk = ws.cell(r - 1, 7, '→ 面额明细' if counts else '（暂无盘点记录）')
            lk.alignment = Alignment(horizontal='left', vertical='center')
            if counts:
                set_internal_link(lk, '现金盘点')
            else:
                lk.font = Font(size=11, color='94A3B8')
            if counts:
                r = table_header(r, [(1, 1, '盘点日期'), (2, 2, '币种'),
                                     (3, 3, '账面应有'), (4, 4, '盘点实有'),
                                     (5, 5, '差异'), (6, 6, '盘点人'),
                                     (7, NC, '备注')])
                for h in counts:
                    items = []
                    for it in db.execute('SELECT * FROM cash_count_items '
                                         'WHERE count_id=? ORDER BY id', (h['id'],)):
                        ds = _denoms_of(db, it['id'])
                        items.append({**rowdict(it),
                                      **_cash_item_calc(rowdict(it), ds)})
                    book_mop = sum(i['book'] * fx(i['currency']) for i in items)
                    act_mop = sum(i['actual'] * fx(i['currency']) for i in items)
                    diff_mop = sum(i['diff'] * fx(i['currency']) for i in items)
                    r = data_row(r, [
                        (1, 1, h['count_date'], 'date'),
                        (2, 2, '/'.join(i['currency'] for i in items), 'center'),
                        (3, 3, round(book_mop, 2), 'money'),
                        (4, 4, round(act_mop, 2), 'money'),
                        (5, 5, round(diff_mop, 2), 'money'),
                        (6, 6, h['person'] or '', 'text'),
                        (7, NC, (f'监盘：{h["supervisor"]}　' if h['supervisor'] else '')
                         + (h['note'] or ''), 'text'),
                    ], height=22)
                    if abs(diff_mop) > 0.005:
                        dc = ws.cell(r - 1, 5)
                        dc.font = Font(bold=True, size=11, color='C0392B')
                r += 1

        # ---------- 标签标记统计（按日期范围）
        tag_cond, tag_params = [], []
        if frm:
            tag_cond.append(' AND e.date>=?')
            tag_params.append(frm)
        if to:
            tag_cond.append(' AND e.date<=?')
            tag_params.append(to)
        acc_ids = [a['id'] for a in accs]
        ph = ','.join('?' for _ in acc_ids) or 'NULL'
        tag_rows = db.execute(
            "SELECT t.name, COUNT(e.id) n, "
            "COALESCE(SUM(CASE WHEN e.pending=1 THEN 1 ELSE 0 END),0) np "
            "FROM tags t "
            "LEFT JOIN entry_tags et ON et.tag_id=t.id "
            "LEFT JOIN entries e ON e.id=et.entry_id "
            f"  AND e.account_id IN ({ph})" +
            ''.join(tag_cond) +
            " GROUP BY t.id ORDER BY t.id",
            acc_ids + tag_params).fetchall()
        if tag_rows:
            r = section(r, '🏷️ 标签标记统计', span=3)
            r = table_header(r, [(1, 3, '标签'), (4, 4, '标记笔数'),
                                 (5, 6, '其中未兑现/未到账'), (7, NC, '说明')])
            for t in tag_rows:
                r = data_row(r, [(1, 3, t['name'], 'text'),
                                 (4, 4, t['n'], 'int'),
                                 (5, 6, t['np'], 'int'),
                                 (7, NC, '', 'text')])

        for i, w in enumerate([6, 34, 8, 20, 24, 20, 15, 15, 10, 17, 16], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.page_setup.orientation = 'landscape'
        ws.page_setup.paperSize = ws.PAPERSIZE_A3
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)

        # ---------- 每个账户一个工作表
        for a, sn in zip(accs, names):
            sh = wb.create_sheet(sn)
            _r, hdr = write_account_block(sh, 1, a, *acc_rng(a))
            finish_journal_ws(sh, hdr)

        if incl_dep:
            write_deposits_sheet(cp['id'])
        if incl_cash:
            write_cash_counts_sheet(cp['id'])
        return wb

    if etype == 'journal':
        aid = request.args.get('account_id', type=int)
        accs = journal_accounts('WHERE a.id=?', (aid,))
        if not accs:
            return bad('账户不存在', 404)
        write_journal_sheet(accs)
        acc_name = re.sub(r'[\\/:*?"<>|]', '_', accs[0]['name'])
        parts = [acc_name]
        if frm or to:
            parts.append(f"{frm or '开始'}至{to or '至今'}")
        fname = f"日记账_{'_'.join(parts)}_{date.today():%Y%m%d}.xlsx"
    elif etype == 'deposits':
        write_deposits_sheet(request.args.get('company_id', type=int))
        fname = f'定期存款_{date.today():%Y%m%d}.xlsx'
    else:
        # 全量导出：默认本月；每个公司一个 Excel，多公司打包 zip
        if not frm and not to and request.args.get('all') not in ('1', 'true', 'yes'):
            _t = date.today()
            frm = _t.replace(day=1).isoformat()
            to = (date(_t.year + _t.month // 12, _t.month % 12 + 1, 1)
                  - timedelta(days=1)).isoformat()
        all_accs = journal_accounts()
        comps_all = db.execute('SELECT * FROM companies ORDER BY sort, id').fetchall()
        rates_map = {x['currency']: x['rate'] for x in
                     db.execute('SELECT * FROM rates')}
        deps = db.execute(
            'SELECT d.*, c.name company_name FROM deposits d '
            'JOIN companies c ON c.id=d.company_id '
            'ORDER BY c.sort, c.id, d.start_date, d.id').fetchall()
        want = [int(x) for x in (request.args.get('companies') or '').split(',')
                if x.strip().isdigit()]
        targets = [c for c in comps_all if not want or c['id'] in want]
        if not targets:
            return bad('没有可导出的公司')
        if frm and to:
            span = f'{frm}至{to}'
        elif frm:
            span = f'{frm}起'
        elif to:
            span = f'截至{to}'
        else:
            span = '全部历史'
        tmpdir = tempfile.mkdtemp()
        files = []
        skipped = []
        for cp in targets:
            book = build_company_book(cp, all_accs, deps, rates_map)
            if book is None:
                skipped.append(cp['name'])
                continue
            for _ws in book.worksheets:
                for _row in _ws.iter_rows():
                    for _cell in _row:
                        _f = _cell.font
                        _cell.font = Font(name=FONT_NAME, size=_f.sz, bold=_f.b,
                                          italic=_f.i, underline=_f.u,
                                          color=_f.color)
            book.calculation.fullCalcOnLoad = True
            safe_cp = re.sub(r'[\\/:*?"<>|]', '_', cp['name'])
            fn = f'{safe_cp}_{span}_日记账.xlsx'
            path = os.path.join(tmpdir, fn)
            book.save(path)
            files.append((fn, path))
        if not files:
            return bad('没有可导出的工作表（选中的公司都没有勾选任何账户页或定期存款页）', 400)
        if len(files) == 1:
            return send_file(files[0][1], as_attachment=True,
                             download_name=files[0][0])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for fn, path in files:
                z.write(path, fn)
        buf.seek(0)
        return send_file(buf, as_attachment=True,
                         download_name=f'日记账_{span}_{date.today():%Y%m%d}.zip',
                         mimetype='application/zip')

    # 全表统一使用对中文友好的字体，并去掉所有斜体
    for _ws in wb.worksheets:
        for _row in _ws.iter_rows():
            for _cell in _row:
                _f = _cell.font
                _cell.font = Font(name=FONT_NAME, size=_f.sz, bold=_f.b,
                                  italic=_f.i, underline=_f.u,
                                  color=_f.color)

    wb.calculation.fullCalcOnLoad = True  # 打开 Excel 时立即重算所有公式
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
    wb.save(tmp.name)
    tmp.close()
    return send_file(tmp.name, as_attachment=True, download_name=fname)


@app.get('/api/backup')
def backup():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(DB_PATH, 'journal.db')
        for fn in os.listdir(UPLOAD_DIR):
            z.write(os.path.join(UPLOAD_DIR, fn), f'uploads/{fn}')
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f'日记账备份_{date.today():%Y%m%d}.zip',
                     mimetype='application/zip')


@app.post('/api/restore')
def restore():
    f = request.files.get('file')
    if not f:
        return bad('请选择备份 zip 文件')
    tmpdir = tempfile.mkdtemp()
    try:
        zf = zipfile.ZipFile(f)
        names = zf.namelist()
        if 'journal.db' not in names:
            return bad('备份文件缺少 journal.db，无法恢复')
        zf.extractall(tmpdir)
    except zipfile.BadZipFile:
        return bad('无法读取 zip 文件')
    with _write_lock:
        shutil.copyfile(os.path.join(tmpdir, 'journal.db'), DB_PATH)
        for fn in os.listdir(UPLOAD_DIR):
            try:
                os.remove(os.path.join(UPLOAD_DIR, fn))
            except OSError:
                pass
        updir = os.path.join(tmpdir, 'uploads')
        if os.path.isdir(updir):
            for fn in os.listdir(updir):
                shutil.copyfile(os.path.join(updir, fn),
                                os.path.join(UPLOAD_DIR, fn))
    shutil.rmtree(tmpdir, ignore_errors=True)
    return jsonify({'ok': True})


# ---------------------------------------------------------------- main
def port_open():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', PORT))
        s.close()
        return False
    except OSError:
        return True


if __name__ == '__main__':
    if os.environ.get('NO_BROWSER') != '1':
        if port_open():
            webbrowser.open(f'http://127.0.0.1:{PORT}')
            print(f'已有实例在运行，已打开浏览器 http://127.0.0.1:{PORT}')
            sys.exit(0)
        threading.Timer(1.2, lambda: webbrowser.open(
            f'http://127.0.0.1:{PORT}')).start()
    print(f'日记账 BI 系统 已启动: http://127.0.0.1:{PORT}')
    app.run(host='127.0.0.1', port=PORT, debug=False)
