# -*- coding: utf-8 -*-
"""一次性数据导入：历史银行流水 CSV + 银行日记账版本1.xlsx（十月流水/定期）→ BI 系统 SQLite
用法：python tools/import_data.py [--force]
幂等保护：数据库已有公司数据时须加 --force 清空重导

> 注意：本脚本是原作者一次性迁移真实账本时用的**参考模板**，里面的文件名、列号、
> schema 都是旧版（缺 kind / opening_date 字段）。照抄运行会建出与 app.py 不一致的表，
> 请先对照自己的表格改列映射，或直接改用 app.py 启动时自动创建的库 + 界面录入。

"""
import csv
import os
import re
import sys
import sqlite3
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE, 'data')
os.makedirs(os.path.join(DATA_DIR, 'uploads'), exist_ok=True)
DB = os.path.join(DATA_DIR, 'journal.db')
ROOT = os.path.dirname(BASE)
CSV_MOP = os.path.join(ROOT, '历史日记账', 'MOP.csv')
CSV_HKD = os.path.join(ROOT, '历史日记账', 'HKD.csv')
XLSX_V1 = os.path.join(ROOT, '银行日记账版本1.xlsx')

DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
SKIP_DESC = {'期初', '本日小计', '总计'}

COMPANY = '示例贸易有限公司'   # TODO: 改成你的公司名
RATES = {'MOP': 1.0, 'HKD': 1.03, 'CNY': 1.2237}
TAGS = [('未收收据', '#f59e0b'), ('未签字', '#ef4444'), ('已核对', '#10b981')]

SCHEMA = '''
CREATE TABLE IF NOT EXISTS companies(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL, name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'MOP', bank TEXT DEFAULT '',
  account_no TEXT DEFAULT '', opening REAL NOT NULL DEFAULT 0,
  sort INTEGER DEFAULT 0);
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
'''


def read_bank_csv(path):
    """返回 (期初余额, entries)；entry: (date, 单据号, 摘要, 对方单位, 借方, 贷方)"""
    opening, entries = 0.0, []
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            if len(row) < 10:
                continue  # 尾部“当前区域共计N行!”等非数据行
            d = (row.get('日期') or '').replace("'", '').strip()
            desc = (row.get('摘要') or '').strip()
            if not DATE_RE.match(d) or desc in SKIP_DESC:
                if desc == '期初':
                    opening = float(row.get('余额_原币') or 0)
                continue
            entries.append((d, (row.get('单据号') or '').strip(), desc,
                            (row.get('对方单位') or '').strip(),
                            float(row.get('收入_原币') or 0),
                            float(row.get('支出_原币') or 0)))
    return opening, entries


def read_v1(xlsx_path):
    """V1：十月流水 + 定期存款"""
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb['202610']
    col_cur = {5: 'MOP', 6: 'HKD', 7: 'CNY'}  # E/F/G
    entries = []
    for r in range(7, ws.max_row + 1):
        dcell = ws.cell(r, 1).value
        if dcell is None:
            continue
        d = dcell.strftime('%Y-%m-%d') if isinstance(dcell, datetime) else str(dcell)[:10]
        if not DATE_RE.match(d):
            continue
        chk = str(ws.cell(r, 2).value or '').strip()
        desc = str(ws.cell(r, 4).value or '').strip()
        if not desc:
            continue
        for c, cur in col_cur.items():
            v = ws.cell(r, c).value
            if not isinstance(v, (int, float)) or v == 0:
                continue
            entries.append((d, chk, desc, '', cur, v if v > 0 else 0, -v if v < 0 else 0))
    deps = []
    ws2 = wb['定期']
    for r in range(2, ws2.max_row + 1):
        if not ws2.cell(r, 3).value or not isinstance(ws2.cell(r, 5).value, (int, float)):
            continue
        def ds(col):
            v = ws2.cell(r, col).value
            return v.strftime('%Y-%m-%d') if isinstance(v, datetime) else str(v or '')
        deps.append(dict(bank=str(ws2.cell(r, 2).value or '示例银行'),
                         order_no=str(ws2.cell(r, 3).value).strip(),
                         currency=str(ws2.cell(r, 4).value or 'HKD'),
                         principal=float(ws2.cell(r, 5).value),
                         period=str(ws2.cell(r, 6).value or ''),
                         rate=float(ws2.cell(r, 8).value or 0),
                         start_date=ds(12), end_date=ds(13), note=''))
    return entries, deps


def main(force=False):
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    if con.execute('SELECT COUNT(*) FROM companies').fetchone()[0] and not force:
        print('数据库已有数据，如需清空重导请加 --force')
        con.close()
        return
    mop_open, mop_entries = read_bank_csv(CSV_MOP)
    hkd_open, hkd_entries = read_bank_csv(CSV_HKD)
    v1_entries, deps = read_v1(XLSX_V1)

    with con:
        if force:
            for t in ('entry_tags', 'attachments', 'entries', 'accounts',
                      'deposits', 'tags', 'companies', 'rates'):
                con.execute(f'DELETE FROM {t}')
        cid = con.execute('INSERT INTO companies(name) VALUES(?)',
                          (COMPANY,)).lastrowid
        accs = {}
        for name, cur, no, opening in [
                ('示例银行葡币支票户', 'MOP', '000-000000-0001', mop_open),
                ('示例银行港币支票户', 'HKD', '000-000000-0002', hkd_open),
                ('人民币储蓄户', 'CNY', '000-000000-0003', 13259.44)]:
            accs[cur] = con.execute(
                'INSERT INTO accounts(company_id,name,currency,bank,account_no,opening) '
                'VALUES(?,?,?,?,?,?)',
                (cid, name, cur, '示例银行', no, opening)).lastrowid
        for k, v in RATES.items():
            con.execute('INSERT OR REPLACE INTO rates(currency,rate) VALUES(?,?)', (k, v))
        for name, color in TAGS:
            con.execute('INSERT INTO tags(name,color) VALUES(?,?)', (name, color))

        n = 0
        for cur, (d, chk, desc, party, debit, credit) in (
                [('MOP', e) for e in mop_entries] + [('HKD', e) for e in hkd_entries]):
            con.execute(
                'INSERT INTO entries(account_id,date,check_no,description,party,'
                'debit,credit,pending,project) VALUES(?,?,?,?,?,?,?,0,"")',
                (accs[cur], d, chk, desc, party, debit, credit))
            n += 1
        for d, chk, desc, party, cur, debit, credit in v1_entries:
            con.execute(
                'INSERT INTO entries(account_id,date,check_no,description,party,'
                'debit,credit,pending,project) VALUES(?,?,?,?,?,?,?,0,"")',
                (accs[cur], d, chk, desc, party, debit, credit))
            n += 1
        for dp in deps:
            con.execute(
                'INSERT INTO deposits(company_id,bank,order_no,currency,principal,'
                'period,rate,start_date,end_date,note) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (cid, dp['bank'], dp['order_no'], dp['currency'], dp['principal'],
                 dp['period'], dp['rate'], dp['start_date'], dp['end_date'], dp['note']))

        # 校验：各账户账面余额应与 V1 汇总一致
        print(f'导入完成：流水 {n} 笔，定期 {len(deps)} 笔')
        con.row_factory = sqlite3.Row
        for row in con.execute(
                "SELECT a.name, a.currency, a.opening, "
                "COALESCE(SUM(e.debit),0) d, COALESCE(SUM(e.credit),0) c "
                "FROM accounts a LEFT JOIN entries e ON e.account_id=a.id "
                "GROUP BY a.id"):
            book = row['opening'] + row['d'] - row['c']
            print(f"  {row['name']}（{row['currency']}）: 期初 {row['opening']:,.2f} "
                  f"→ 账面 {book:,.2f}")
    con.close()


if __name__ == '__main__':
    main(force='--force' in sys.argv)
