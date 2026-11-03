# -*- coding: utf-8 -*-
"""导入库存现金：现金日记账.xlsx（期初）+ 现金盘点表.xls（盘点记录）
用法：python tools/import_cash.py [--force]
  --force  先删除该公司已有的现金账户（含流水）与全部盘点单，再重建

数据契约
  现金日记账.xlsx ：「月末余额」行 D2=单位名；第 3 行表头；第 4 行「XXXX年X月 期初余额：」
                     E=葡幣MOP  F=港幣HKD  G=人民幣CNY；第 5 行起为流水
  现金盘点表.xls  ：H 列=面額(元)，I/J=人民币张数/金额，K/L=澳门币张数/金额，M/N=港元张数/金额
                     盘点日期在 A3「盤點日期： 2026年10月30日」，盘点人在 A27

> 注意：本脚本是原作者一次性迁移真实账本时用的**参考模板**，里面的文件名、列号、
> schema 都是旧版（缺 kind / opening_date 字段）。照抄运行会建出与 app.py 不一致的表，
> 请先对照自己的表格改列映射，或直接改用 app.py 启动时自动创建的库 + 界面录入。

"""
import os
import re
import sys
import sqlite3
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE, 'data')
DB = os.path.join(DATA_DIR, 'journal.db')
ROOT = os.path.dirname(BASE)
XLSX_CASH = os.path.join(ROOT, '现金日记账.xlsx')
XLS_COUNT = os.path.join(ROOT, '2026年10月现金盘点表 .xls')

COMPANY_KEY = '示例科技'          # 按名称模糊匹配现有公司，改成你自己的
CUR_COLS = {5: 'MOP', 6: 'HKD', 7: 'CNY'}      # E/F/G（1-based）
ACC_NAME = {'MOP': '库存现金（澳门币）', 'HKD': '库存现金（港币）',
            'CNY': '库存现金（人民币）'}
CUR_ORDER = ['MOP', 'HKD', 'CNY']


def find_company(con):
    rows = con.execute('SELECT id,name FROM companies').fetchall()
    hit = [r for r in rows if COMPANY_KEY in r[1]]
    if not hit:
        raise SystemExit(f'找不到公司「{COMPANY_KEY}」，现有：{[r[1] for r in rows]}')
    return hit[0]


def read_cash_book(path):
    """返回 (期初 dict{cur:amt}, 期初所属日期, 流水 list)"""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    opening, open_date, entries = {}, '', []
    head = str(ws.cell(4, 4).value or '')
    m = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月', head)
    if m:
        open_date = f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-01'
    for c, cur in CUR_COLS.items():
        v = ws.cell(4, c).value
        opening[cur] = float(v) if isinstance(v, (int, float)) else 0.0
    for r in range(5, ws.max_row + 1):
        dc = ws.cell(r, 1).value
        desc = str(ws.cell(r, 4).value or '').strip()
        if dc is None or not desc:
            continue
        d = dc.strftime('%Y-%m-%d') if isinstance(dc, datetime) else str(dc)[:10]
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            continue
        for c, cur in CUR_COLS.items():
            v = ws.cell(r, c).value
            if not isinstance(v, (int, float)) or not v:
                continue
            entries.append((d, str(ws.cell(r, 2).value or '').strip(), desc, cur,
                            v if v > 0 else 0.0, -v if v < 0 else 0.0))
    return opening, open_date, entries


def read_count_sheet(path):
    """返回 (盘点日期, 盘点人, {cur: {面额: 张数}})"""
    import xlrd
    wb = xlrd.open_workbook(path)
    ws = wb.sheets()[0]
    date_txt = str(ws.cell_value(2, 0) or '')
    m = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', date_txt)
    count_date = (f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
                  if m else '')
    person = ''
    for r in range(ws.nrows):
        t = str(ws.cell_value(r, 0) or '')
        if '盤點人' in t or '盘点人' in t:
            person = re.split(r'[：:]', t)[-1].strip()
            break
    cols = {'CNY': (8, 9), 'MOP': (10, 11), 'HKD': (12, 13)}   # 0-based: 张数, 金额
    denoms = {}
    for r in range(ws.nrows):
        dv = ws.cell_value(r, 7)          # H 列 面額
        if not isinstance(dv, (int, float)) or not dv:
            continue
        for cur, (qc, _ac) in cols.items():
            q = ws.cell_value(r, qc)
            if isinstance(q, (int, float)) and q:
                denoms.setdefault(cur, {})[float(dv)] = float(q)
    return count_date, person, denoms


def ensure_schema(con):
    """补齐 app.py 新增的列/表（不依赖先启动一次服务）"""
    acols = [r[1] for r in con.execute('PRAGMA table_info(accounts)')]
    if 'kind' not in acols:
        con.execute("ALTER TABLE accounts ADD COLUMN kind TEXT NOT NULL DEFAULT 'bank'")
    if 'opening_date' not in acols:
        con.execute("ALTER TABLE accounts ADD COLUMN opening_date TEXT DEFAULT ''")
    con.executescript('''
CREATE TABLE IF NOT EXISTS cash_counts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL,
  count_date TEXT NOT NULL, person TEXT DEFAULT '', supervisor TEXT DEFAULT '',
  note TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS cash_count_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, count_id INTEGER NOT NULL,
  currency TEXT NOT NULL, prev_book REAL DEFAULT 0, unposted_in REAL DEFAULT 0,
  unposted_out REAL DEFAULT 0, actual REAL DEFAULT 0, white REAL DEFAULT 0,
  reason TEXT DEFAULT '', adj_paid REAL DEFAULT 0, report_book REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cash_count_denoms(
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
  denom REAL NOT NULL, qty REAL DEFAULT 0);
    ''')
    con.commit()


def main(force=False):
    opening, open_date, entries = read_cash_book(XLSX_CASH)
    count_date, person, denoms = read_count_sheet(XLS_COUNT)
    print(f'现金期初（{open_date or "未注明日期"}）：'
          + '　'.join(f'{k} {opening[k]:,.2f}' for k in CUR_ORDER))
    print(f'现金流水 {len(entries)} 笔；盘点表 {count_date} 盘点人 {person or "—"}')
    for cur in CUR_ORDER:
        d = denoms.get(cur, {})
        if d:
            tot = sum(k * v for k, v in d.items())
            print(f'  面额 {cur}: ' + ' '.join(f'{k:g}×{v:g}' for k, v in
                                             sorted(d.items(), reverse=True))
                  + f'　合计 {tot:,.2f}')

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    cid, cname = find_company(con)
    print(f'目标公司：#{cid} {cname}')

    with con:
        if force:
            ids = [r['id'] for r in con.execute(
                "SELECT id FROM accounts WHERE company_id=? AND kind='cash'", (cid,))]
            for aid in ids:
                con.execute('DELETE FROM entry_tags WHERE entry_id IN '
                            '(SELECT id FROM entries WHERE account_id=?)', (aid,))
                con.execute('DELETE FROM entries WHERE account_id=?', (aid,))
                con.execute('DELETE FROM accounts WHERE id=?', (aid,))
            con.execute('DELETE FROM cash_count_denoms WHERE item_id IN '
                        '(SELECT id FROM cash_count_items WHERE count_id IN '
                        '(SELECT id FROM cash_counts WHERE company_id=?))', (cid,))
            con.execute('DELETE FROM cash_count_items WHERE count_id IN '
                        '(SELECT id FROM cash_counts WHERE company_id=?)', (cid,))
            con.execute('DELETE FROM cash_counts WHERE company_id=?', (cid,))
            print(f'已清除现金账户 {len(ids)} 个及历史盘点单')

        # ---------- 现金账户
        accs = {}
        for cur in CUR_ORDER:
            row = con.execute(
                "SELECT * FROM accounts WHERE company_id=? AND kind='cash' "
                "AND currency=?", (cid, cur)).fetchone()
            if row:
                con.execute('UPDATE accounts SET name=?, bank=?, opening=?, '
                            'opening_date=? WHERE id=?',
                            (ACC_NAME[cur], '现金库', opening[cur],
                             open_date, row['id']))
                accs[cur] = row['id']
            else:
                accs[cur] = con.execute(
                    'INSERT INTO accounts(company_id,name,currency,bank,account_no,'
                    "opening,kind,opening_date,sort) "
                    "VALUES(?,?,?,'现金库','',?,'cash',?,?)",
                    (cid, ACC_NAME[cur], cur, opening[cur], open_date,
                     100 + CUR_ORDER.index(cur))).lastrowid
        print('现金账户：' + '、'.join(f'{ACC_NAME[c]}(#{accs[c]})' for c in CUR_ORDER))

        n = 0
        for d, no, desc, cur, debit, credit in entries:
            con.execute(
                'INSERT INTO entries(account_id,date,check_no,description,party,'
                'debit,credit,pending,project) VALUES(?,?,?,?,?,?,?,0,"")',
                (accs[cur], d, no, desc, '', debit, credit))
            n += 1
        if n:
            print(f'已导入现金流水 {n} 笔')

        # ---------- 盘点单（同日期已存在则覆盖明细）
        old = con.execute('SELECT id FROM cash_counts WHERE company_id=? '
                          'AND count_date=?', (cid, count_date)).fetchone()
        if old:
            kc_id = old['id']
            con.execute('UPDATE cash_counts SET person=?, supervisor=? WHERE id=?',
                        (person, '', kc_id))
            con.execute('DELETE FROM cash_count_denoms WHERE item_id IN '
                        '(SELECT id FROM cash_count_items WHERE count_id=?)', (kc_id,))
            con.execute('DELETE FROM cash_count_items WHERE count_id=?', (kc_id,))
        else:
            kc_id = con.execute(
                'INSERT INTO cash_counts(company_id,count_date,person,supervisor,'
                'note,created_at) VALUES(?,?,?,"","由现金盘点表导入",?)',
                (cid, count_date, person, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            ).lastrowid

        for cur in CUR_ORDER:
            q = denoms.get(cur, {})
            # 账面：取该币种现金账户截至盘点日的余额（期初 + 已入账流水）
            aid = accs[cur]
            agg = con.execute(
                'SELECT COALESCE(SUM(debit),0) d, COALESCE(SUM(credit),0) c '
                'FROM entries WHERE account_id=? AND pending=0 AND date<=?',
                (aid, count_date)).fetchone()
            book = opening[cur] + agg['d'] - agg['c']
            actual = round(sum(k * v for k, v in q.items()), 2)
            item_id = con.execute(
                'INSERT INTO cash_count_items(count_id,currency,prev_book,'
                'unposted_in,unposted_out,actual,white,reason,adj_paid,report_book) '
                'VALUES(?,?,?,0,0,?,0,"",0,?)',
                (kc_id, cur, book, actual, book)).lastrowid
            for dv, qv in q.items():
                con.execute('INSERT INTO cash_count_denoms(item_id,denom,qty) '
                            'VALUES(?,?,?)', (item_id, dv, qv))
            print(f'  盘点 {cur}: 账面 {book:,.2f}　实有 {actual:,.2f}　'
                  f'差异 {actual - book:,.2f}')

    # ---------- 校验
    print('\n校验：')
    for cur in CUR_ORDER:
        r = con.execute(
            'SELECT a.opening, COALESCE(SUM(e.debit),0) d, COALESCE(SUM(e.credit),0) c '
            'FROM accounts a LEFT JOIN entries e ON e.account_id=a.id '
            'WHERE a.id=? GROUP BY a.id', (accs[cur],)).fetchone()
        print(f'  {ACC_NAME[cur]}: 期初 {r["opening"]:,.2f} → '
              f'账面 {r["opening"] + r["d"] - r["c"]:,.2f}')
    con.close()


if __name__ == '__main__':
    main(force='--force' in sys.argv)
