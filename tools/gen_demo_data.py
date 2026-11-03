# -*- coding: utf-8 -*-
"""生成演示数据 —— 全虚构，可安全公开

用法：
    python tools/gen_demo_data.py            # data/journal.db 不存在时生成
    python tools/gen_demo_data.py --force    # 覆盖已有 data/journal.db

生成内容：
    * 3 家虚构公司（示例贸易 / 示例科技 / 示例工程）
    * 每家 3 个银行账户 + 2 个库存现金账户
    * 2025-01 ~ 2026-10 共 22 个月的流水，约 700 笔
    * 若干港币定期存单、现金盘点单、标签

表结构不重复定义：直接从 app.py 里读取 SCHEMA 常量，
保证演示库与正式库结构永远一致。
"""
import argparse
import ast
import os
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'journal.db')

random.seed(20260101)          # 固定种子：每次生成的数据一样，方便对文档


def load_schema():
    """从 app.py 里取出 SCHEMA，避免两份 schema 走样。"""
    app_py = os.path.join(BASE_DIR, 'app.py')
    with open(app_py, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, 'id', '') == 'SCHEMA':
                    return ast.literal_eval(node.value)
    raise RuntimeError('未能在 app.py 中找到 SCHEMA 常量')


COMPANIES = [
    '示例贸易有限公司',
    '示例科技有限公司',
    '示例工程有限公司',
]

# 虚构往来单位 —— 一律带「示例」前缀，一眼可辨
CUSTOMERS = ['示例客户甲', '示例客户乙', '示例客户丙', '示例客户丁']
SUPPLIERS = ['示例供应商甲', '示例供应商乙', '示例供应商丙']
BANKS = [('示例银行', '000-000000-0001'),
         ('示例银行', '000-000000-0002'),
         ('示例银行', '000-000000-0003')]

CUR_BANK = [('MOP', '葡币支票户'), ('HKD', '港币支票户'), ('CNY', '人民币储蓄户')]
CUR_CASH = [('MOP', '库存现金-葡币'), ('HKD', '库存现金-港币')]

TAGS = [('未收收据', '#f59e0b'), ('未签字', '#ef4444'),
        ('已通知收票', '#3b82f6'), ('待对账', '#8b5cf6')]

# 收入模板：(描述模板, 金额区间)
INCOME = [
    ('收{party}{m}月服务费', 80_000, 620_000),
    ('收{party}{m}月货款', 50_000, 450_000),
    ('收{party}{m}月管理费分成', 30_000, 180_000),
    ('收{party}往来款', 10_000, 120_000),
]
# 支出模板
EXPENSE = [
    ('付{party}{m}月货款', 40_000, 380_000),
    ('付{party}{m}月外包服务费', 20_000, 160_000),
    ('付{m}月租金', 25_000, 90_000),
    ('付{m}月水电费', 3_000, 26_000),
    ('付{m}月员工工资', 120_000, 320_000),
    ('付{m}月社保及职业税', 8_000, 40_000),
    ('付{m}月银行手续费', 200, 3_500),
    ('付{m}月办公用品', 500, 12_000),
    ('付{m}月差旅费报销', 2_000, 45_000),
    ('付{m}月审计费', 15_000, 60_000),
]


def money(lo, hi):
    """两位小数的金额，量级分布更自然（小额多、大额少）。"""
    x = random.random() ** 1.8
    return round(lo + (hi - lo) * x, 2)


def months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def build(con):
    con.executescript(load_schema())

    rates = [('MOP', 1.0), ('HKD', 1.03), ('CNY', 1.2237), ('USD', 8.0)]
    con.executemany('INSERT OR REPLACE INTO rates(currency, rate) VALUES(?,?)', rates)

    tag_ids = []
    for name, color in TAGS:
        tag_ids.append(con.execute(
            'INSERT INTO tags(name, color) VALUES(?,?)', (name, color)).lastrowid)

    start, end = date(2025, 1, 1), date(2026, 10, 31)
    now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    n_entries = 0

    for ci, cname in enumerate(COMPANIES, start=1):
        cid = con.execute('INSERT INTO companies(name, sort) VALUES(?,?)',
                          (cname, ci)).lastrowid

        bank_acc, cash_acc = [], []
        for si, (cur, label) in enumerate(CUR_BANK):
            opening = round(random.uniform(80_000, 2_600_000), 2)
            bank_acc.append((cur, con.execute(
                'INSERT INTO accounts(company_id, name, currency, bank, account_no,'
                ' opening, sort, kind, opening_date) VALUES(?,?,?,?,?,?,?,?,?)',
                (cid, label, cur, BANKS[si % len(BANKS)][0],
                 '000-000000-00%02d' % (ci * 10 + si), opening, si,
                 'bank', '')).lastrowid))
        for si, (cur, label) in enumerate(CUR_CASH):
            opening = round(random.uniform(2_000, 30_000), 2)
            cash_acc.append((cur, con.execute(
                'INSERT INTO accounts(company_id, name, currency, bank, account_no,'
                ' opening, sort, kind, opening_date) VALUES(?,?,?,?,?,?,?,?,?)',
                (cid, label, cur, '', '', opening, 10 + si,
                 'cash', '2025-01-01')).lastrowid))

        # ---- 银行流水：每月 20~28 笔，收入约占四成
        for y, m in months(start, end):
            n = random.randint(20, 28)
            last_day = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)).day
            for _ in range(n):
                d = date(y, m, random.randint(1, last_day))
                if random.random() < 0.42:
                    tpl, lo, hi = random.choice(INCOME)
                    party = random.choice(CUSTOMERS)
                    desc = tpl.format(party=party, m=m)
                    debit, credit = money(lo, hi), 0.0
                else:
                    tpl, lo, hi = random.choice(EXPENSE)
                    party = random.choice(SUPPLIERS) if '{party}' in tpl else ''
                    desc = tpl.format(party=party, m=m)
                    debit, credit = 0.0, money(lo, hi)
                # 收入多落在葡币户（第一本），支出分散在各账户
                acc = bank_acc[0][1] if (debit and random.random() < 0.6) \
                    else random.choice(bank_acc)[1]
                eid = con.execute(
                    'INSERT INTO entries(account_id, date, check_no, description, party,'
                    ' debit, credit, pending, project, created_at)'
                    ' VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (acc, d.isoformat(),
                     str(random.randint(10_000_000, 99_999_999)) if random.random() < .7 else '',
                     desc, party, debit, credit,
                     1 if random.random() < 0.07 else 0,
                     random.choice(['', '', '', '示例项目A', '示例项目B']),
                     now_ts)).lastrowid
                n_entries += 1
                if random.random() < 0.06:
                    con.execute('INSERT OR IGNORE INTO entry_tags(entry_id, tag_id) '
                                'VALUES(?,?)', (eid, random.choice(tag_ids)))

        # ---- 现金流水：每月 1~4 笔小额
        for y, m in months(start, end):
            for _ in range(random.randint(1, 4)):
                d = date(y, m, random.randint(1, 28))
                cur, acc = random.choice(cash_acc)
                out = random.random() < 0.6
                eid = con.execute(
                    'INSERT INTO entries(account_id, date, check_no, description, party,'
                    ' debit, credit, pending, project, created_at)'
                    ' VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (acc, d.isoformat(), '',
                     ('备用金支出：办公杂费' if out else '现金收入：零星收款'),
                     '', 0.0 if out else money(200, 6_000),
                     money(200, 6_000) if out else 0.0, 0, '', now_ts)).lastrowid
                n_entries += 1

        # ---- 修正期初余额，保证每个账户「期初」和「期末」都是正数
        #     opening = max(0, -净流量) + 随机缓冲 → 期末必然为正
        for cur, acc in bank_acc:
            net = con.execute(
                'SELECT COALESCE(SUM(debit),0)-COALESCE(SUM(credit),0) FROM entries '
                'WHERE account_id=?', (acc,)).fetchone()[0]
            opening = round(max(0.0, -net) + random.uniform(200_000, 1_200_000), 2)
            con.execute('UPDATE accounts SET opening=? WHERE id=?', (opening, acc))
        for cur, acc in cash_acc:
            net = con.execute(
                'SELECT COALESCE(SUM(debit),0)-COALESCE(SUM(credit),0) FROM entries '
                'WHERE account_id=?', (acc,)).fetchone()[0]
            opening = round(max(0.0, -net) + random.uniform(3_000, 18_000), 2)
            con.execute('UPDATE accounts SET opening=? WHERE id=?', (opening, acc))

        # ---- 定期存单（港币）
        for k in range(random.randint(1, 3)):
            sd = date(2026, random.randint(1, 9), random.randint(1, 28))
            con.execute(
                'INSERT INTO deposits(company_id, bank, order_no, currency, principal,'
                ' period, rate, start_date, end_date, note) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (cid, '示例银行', '示例-%04d' % (ci * 100 + k), 'HKD',
                 round(random.uniform(800_000, 6_000_000), 2),
                 random.choice(['3M', '6M', '12M']), random.choice([0.021, 0.024, 0.028]),
                 sd.isoformat(), (sd + timedelta(days=random.choice([90, 180, 365]))).isoformat(),
                 ''))

        # ---- 现金盘点单（实有 = 面额合计，账面 = 上一日余额）
        denoms = [1000, 500, 100, 50, 20, 10, 5, 2, 1, 0.5, 0.2, 0.1]
        for days_ago in (3, 32):
            cd = date(2026, 10, 31) - timedelta(days=days_ago)
            count_id = con.execute(
                'INSERT INTO cash_counts(company_id, count_date, person, supervisor,'
                ' note, created_at) VALUES(?,?,?,?,?,?)',
                (cid, cd.isoformat(), '示例员工', '示例主管', '演示数据', now_ts)).lastrowid
            for cur, _acc in cash_acc:
                prev = round(random.uniform(2_000, 25_000), 2)
                actual, white = 0.0, 0.0
                item_id = con.execute(
                    'INSERT INTO cash_count_items(count_id, currency, prev_book,'
                    ' unposted_in, unposted_out, actual, white, reason, adj_paid,'
                    ' report_book) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (count_id, cur, prev, 0, 0, 0, 0, '',
                     0, prev)).lastrowid
                remain = prev
                for dn in denoms:
                    if dn > remain:
                        continue
                    q = int(remain // dn * random.uniform(0.4, 1.0))
                    if dn >= 50:
                        q = min(q, 60)
                    if q:
                        con.execute('INSERT INTO cash_count_denoms(item_id, denom, qty)'
                                    ' VALUES(?,?,?)', (item_id, dn, q))
                        actual += dn * q
                        remain -= dn * q
                actual = round(actual, 2)
                con.execute('UPDATE cash_count_items SET actual=?, white=? WHERE id=?',
                            (actual, white, item_id))

    con.commit()
    return n_entries


def main():
    ap = argparse.ArgumentParser(description='生成演示数据（全虚构）')
    ap.add_argument('--force', action='store_true', help='覆盖已存在的 journal.db')
    args = ap.parse_args()

    if os.path.exists(DB_PATH) and not args.force:
        print(f'已存在 {DB_PATH}，跳过。要覆盖请加 --force')
        return 0

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, 'uploads'), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    con = sqlite3.connect(DB_PATH)
    try:
        n = build(con)
    finally:
        con.close()
    print(f'演示数据已生成：{DB_PATH}')
    print(f'  公司 {len(COMPANIES)} 家，流水 {n} 笔，全部为虚构数据')
    return 0


if __name__ == '__main__':
    sys.exit(main())
