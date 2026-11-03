# -*- coding: utf-8 -*-
"""AI Agent —— 自然语言查账

设计要点
--------
* 只依赖标准库（urllib），不新增第三方包，保持「拷贝文件夹就能跑」。
* 兼容一切 OpenAI 格式的 /chat/completions 接口（DeepSeek / OpenAI / 通义 /
  智谱 / Kimi / 硅基流动 / Ollama / 任意自建网关）。
* Agent 循环：模型 → 工具调用 → 回填结果 → 再问模型，直到给出最终回答。
* 工具全部只读（SQLite 以 mode=ro 打开），不会改动账本。
* 每个工具结果自动附带「链接」，前端渲染成可点击按钮；另提供 show_links
  让模型可以主动推送链接给用户。
"""
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime

from flask import Blueprint, jsonify, request

# ---------------------------------------------------------------- 路径 / 配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'journal.db')
CONFIG_PATH = os.path.join(DATA_DIR, 'agent_config.json')

# 链接数量控制：宁可少给几条「对得上的」，也不要一次刷屏
LINK_PER_TOOL = 4   # 单个工具最多贡献几条链接
LINK_TOTAL = 6      # 一轮回答最多显示几条
LINK_MODEL_MAX = 5  # 模型用 show_links 主动推送时最多几条

DEFAULT_CONFIG = {
    'provider': 'deepseek',
    'base_url': 'https://api.deepseek.com/v1',
    'model': 'deepseek-chat',
    'api_key': '',
    'temperature': 0.2,
    'max_rounds': 6,
    'timeout': 90,
}

# 预设：用户选一个就自动填好 base_url + model，仍可手动改
PRESETS = [
    {'id': 'deepseek', 'name': 'DeepSeek（国内直连）',
     'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-chat',
     'key_url': 'https://platform.deepseek.com/api_keys',
     'note': 'deepseek-chat 便宜够用；deepseek-reasoner 推理更强但更慢'},
    {'id': 'openai', 'name': 'OpenAI',
     'base_url': 'https://api.openai.com/v1', 'model': 'gpt-4o-mini',
     'key_url': 'https://platform.openai.com/api-keys',
     'note': '国内需自备代理；可换成 gpt-4o / gpt-4.1'},
    {'id': 'dashscope', 'name': '通义千问（阿里云百炼）',
     'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
     'model': 'qwen-plus',
     'key_url': 'https://bailian.console.aliyun.com/',
     'note': '兼容模式，qwen-plus / qwen-max / qwen-turbo'},
    {'id': 'zhipu', 'name': '智谱 GLM',
     'base_url': 'https://open.bigmodel.cn/api/paas/v4',
     'model': 'glm-4-flash',
     'key_url': 'https://bigmodel.cn/usercenter/apikeys',
     'note': 'glm-4-flash 免费额度大'},
    {'id': 'moonshot', 'name': 'Kimi（月之暗面）',
     'base_url': 'https://api.moonshot.cn/v1', 'model': 'moonshot-v1-8k',
     'key_url': 'https://platform.moonshot.cn/console/api-keys',
     'note': '长上下文，适合整本账的宏观提问'},
    {'id': 'siliconflow', 'name': '硅基流动 SiliconFlow',
     'base_url': 'https://api.siliconflow.cn/v1',
     'model': 'Qwen/Qwen2.5-72B-Instruct',
     'key_url': 'https://cloud.siliconflow.cn/account/ak',
     'note': '聚合多家开源模型，便宜'},
    {'id': 'ollama', 'name': 'Ollama（本地，无需 Key）',
     'base_url': 'http://127.0.0.1:11434/v1', 'model': 'qwen2.5:7b',
     'key_url': 'https://ollama.com/download',
     'note': '本地跑，数据不出机器；需先 ollama pull 模型'},
    {'id': 'custom', 'name': '自定义 / 公司网关',
     'base_url': '', 'model': '', 'key_url': '', 'note': '填任意 OpenAI 兼容地址'},
]


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg.update(json.load(f) or {})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def mask_key(k):
    if not k:
        return ''
    if len(k) <= 10:
        return k[:2] + '****'
    return k[:6] + '****' + k[-4:]


# ---------------------------------------------------------------- 数据库（只读）
def db():
    con = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def rows(sql, params=()):
    con = db()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def one(sql, params=()):
    r = rows(sql, params)
    return r[0] if r else None


def money(v):
    return round(float(v or 0), 2)


# ---------------------------------------------------------------- 工具实现
MAX_ROWS = 200


def _acct_balance(aid, as_of=None):
    """账户余额：期初 + Σ(收-支)；同时给出不含未兑现/未到账的实际余额"""
    a = one('SELECT * FROM accounts WHERE id=?', (aid,))
    if not a:
        return None
    cond, p = 'account_id=?', [aid]
    if as_of:
        cond += ' AND date<=?'
        p.append(as_of)
    s = one(f'SELECT SUM(debit) d, SUM(credit) c FROM entries WHERE {cond}', p)
    b = one(f'SELECT SUM(debit) d, SUM(credit) c FROM entries '
            f'WHERE {cond} AND pending=0', p)
    opening = float(a['opening'] or 0)
    return {
        'opening': money(opening),
        'balance': money(opening + (s['d'] or 0) - (s['c'] or 0)),
        'actual_balance': money(opening + (b['d'] or 0) - (b['c'] or 0)),
        'pending_net': money((s['d'] or 0) - (s['c'] or 0)
                             - (b['d'] or 0) + (b['c'] or 0)),
    }


def t_list_companies(args):
    data = rows('SELECT id, name FROM companies ORDER BY sort, id')
    return {'companies': data, 'count': len(data)}, []


def t_list_accounts(args):
    cid = args.get('company_id')
    sql = ('SELECT a.*, c.name company_name FROM accounts a '
           'JOIN companies c ON c.id=a.company_id')
    p = []
    if cid:
        sql += ' WHERE a.company_id=?'
        p.append(int(cid))
    sql += ' ORDER BY a.company_id, a.sort, a.id'
    out, links = [], []
    for a in rows(sql, p):
        bal = _acct_balance(a['id']) or {}
        out.append({
            'account_id': a['id'], 'company': a['company_name'],
            'name': a['name'], 'currency': a['currency'],
            'kind': a['kind'], 'bank': a['bank'],
            'account_no': a['account_no'],
            'opening': money(a['opening']), 'balance': bal.get('balance'),
            'actual_balance': bal.get('actual_balance'),
        })
        if len(links) < LINK_PER_TOOL:
            links.append({'label': f"{a['company_name']} · {a['name']}（{a['currency']}）",
                          'target': f"journal:{a['id']}"})
    return {'accounts': out, 'count': len(out)}, links


def t_query_entries(args):
    cond, p = [], []
    if args.get('account_id'):
        cond.append('e.account_id=?')
        p.append(int(args['account_id']))
    if args.get('company_id'):
        cond.append('a.company_id=?')
        p.append(int(args['company_id']))
    if args.get('date_from'):
        cond.append('e.date>=?')
        p.append(str(args['date_from']))
    if args.get('date_to'):
        cond.append('e.date<=?')
        p.append(str(args['date_to']))
    kw = str(args.get('keyword') or '').strip()
    if kw:
        cond.append('(e.description LIKE ? OR e.check_no LIKE ? OR e.party LIKE ?'
                    ' OR e.project LIKE ? OR e.note LIKE ?)')
        p += [f'%{kw}%'] * 5
    if args.get('party'):
        cond.append('e.party LIKE ?')
        p.append(f"%{args['party']}%")
    if args.get('project'):
        cond.append('e.project LIKE ?')
        p.append(f"%{args['project']}%")
    if args.get('tag'):
        cond.append('e.id IN (SELECT et.entry_id FROM entry_tags et '
                    'JOIN tags t ON t.id=et.tag_id WHERE t.name LIKE ?)')
        p.append(f"%{args['tag']}%")
    direction = str(args.get('direction') or '').lower()
    if direction in ('in', 'income', '收', '收入'):
        cond.append('e.debit>0')
    elif direction in ('out', 'expense', '支', '支出'):
        cond.append('e.credit>0')
    if args.get('min_amount'):
        cond.append('(e.debit>=? OR e.credit>=?)')
        p += [float(args['min_amount'])] * 2
    status = str(args.get('status') or '').lower()
    if status in ('pending', 'uncleared', '未兑现', '未到账'):
        cond.append('e.pending=1')
    elif status in ('booked', 'cleared', '已入账', '已兑现'):
        cond.append('e.pending=0')
    where = ('WHERE ' + ' AND '.join(cond)) if cond else ''
    base = (f'FROM entries e JOIN accounts a ON a.id=e.account_id '
            f'JOIN companies c ON c.id=a.company_id {where}')
    agg = one(f'SELECT COUNT(*) n, SUM(e.debit) d, SUM(e.credit) c, '
              f'MIN(e.date) mn, MAX(e.date) mx {base}', p) or {}
    limit = int(args.get('limit') or 30)
    limit = max(1, min(limit, MAX_ROWS))
    order = 'ASC' if str(args.get('order') or '').lower() == 'asc' else 'DESC'
    rr = rows(f'SELECT e.*, a.name account_name, a.currency, '
              f'c.name company_name {base} '
              f'ORDER BY e.date {order}, e.id {order} LIMIT {limit}', p)
    links, out = [], []
    for r in rr:
        target = f"entry:{r['id']}:{r['account_id']}"
        row = {
            'entry_id': r['id'], 'date': r['date'],
            'company': r['company_name'], 'account': r['account_name'],
            'currency': r['currency'], 'check_no': r['check_no'],
            'description': r['description'], 'party': r['party'],
            'project': r['project'], 'note': r['note'],
            'debit_in': money(r['debit']), 'credit_out': money(r['credit']),
            'pending': bool(r['pending']),
            'link': target,   # 供模型在 show_links 里原样引用，不要写进正文
        }
        out.append(row)
        kind = '未兑现' if r['pending'] else '已入账'
        amt = (f"收 {money(r['debit']):,.2f}" if r['debit']
               else f"支 {money(r['credit']):,.2f}")
        if len(links) < LINK_PER_TOOL:
            links.append({
                'label': f"{r['date']} {r['description'] or r['check_no'] or '流水'}"
                         f" · {amt} {r['currency']} · {kind}",
                'target': target,
            })
    return {
        'total': agg.get('n') or 0,
        'sum_in': money(agg.get('d')),
        'sum_out': money(agg.get('c')),
        'net': money((agg.get('d') or 0) - (agg.get('c') or 0)),
        'date_min': agg.get('mn'), 'date_max': agg.get('mx'),
        'returned': len(out), 'entries': out,
    }, links


def t_account_balance(args):
    aid = args.get('account_id')
    if not aid:
        return {'error': '缺少 account_id，可先调用 list_accounts'}, []
    a = one('SELECT a.*, c.name company_name FROM accounts a '
            'JOIN companies c ON c.id=a.company_id WHERE a.id=?', (int(aid),))
    if not a:
        return {'error': f'账户 {aid} 不存在'}, []
    as_of = args.get('as_of') or None
    bal = _acct_balance(int(aid), as_of)
    return {
        'company': a['company_name'], 'account': a['name'],
        'currency': a['currency'], 'kind': a['kind'],
        'account_no': a['account_no'], 'as_of': as_of or date.today().isoformat(),
        **bal,
        'note': 'actual_balance 已剔除未兑现/未到账；balance 为账面余额',
    }, [{'label': f"{a['company_name']} · {a['name']} 日记账",
         'target': f'journal:{aid}'}]


def t_company_summary(args):
    cid = args.get('company_id')
    df, dt = args.get('date_from'), args.get('date_to')
    companies = rows('SELECT id, name FROM companies ORDER BY sort, id')
    if cid:
        companies = [c for c in companies if c['id'] == int(cid)]
    out, links = [], []
    for c in companies:
        accs = rows('SELECT * FROM accounts WHERE company_id=? '
                    'ORDER BY sort, id', (c['id'],))
        by_cur = {}
        for a in accs:
            bal = _acct_balance(a['id'], dt) or {}
            cur = a['currency']
            k = f"{cur}｜{'银行' if a['kind'] == 'bank' else '现金'}"
            if k not in by_cur:
                by_cur[k] = {'currency': cur, 'kind': a['kind'],
                             'balance': 0.0, 'actual_balance': 0.0,
                             'accounts': 0}
            by_cur[k]['balance'] += bal.get('balance') or 0
            by_cur[k]['actual_balance'] += bal.get('actual_balance') or 0
            by_cur[k]['accounts'] += 1
        cond, p = ['a.company_id=?'], [c['id']]
        if df:
            cond.append('e.date>=?')
            p.append(df)
        if dt:
            cond.append('e.date<=?')
            p.append(dt)
        flow = rows(f'SELECT a.currency cur, SUM(e.debit) d, SUM(e.credit) c, '
                    f'COUNT(*) n FROM entries e JOIN accounts a ON a.id=e.account_id '
                    f'WHERE {" AND ".join(cond)} GROUP BY a.currency', p)
        out.append({
            'company_id': c['id'], 'company': c['name'],
            'period': {'from': df or '(全部)', 'to': dt or '(全部)'},
            'balances': [{**v, 'balance': money(v['balance']),
                          'actual_balance': money(v['actual_balance'])}
                         for v in by_cur.values()],
            'flows': [{'currency': f['cur'], 'count': f['n'],
                       'in': money(f['d']), 'out': money(f['c']),
                       'net': money((f['d'] or 0) - (f['c'] or 0))}
                      for f in flow],
        })
        if len(links) < LINK_PER_TOOL:
            links.append({'label': f"{c['name']} 总览",
                          'target': f"dashboard:{c['id']}"})
    return {'summaries': out}, links


def t_list_deposits(args):
    cond, p = [], []
    if args.get('company_id'):
        cond.append('d.company_id=?')
        p.append(int(args['company_id']))
    where = ('WHERE ' + ' AND '.join(cond)) if cond else ''
    rr = rows(f'SELECT d.*, c.name company_name FROM deposits d '
              f'JOIN companies c ON c.id=d.company_id {where} '
              f'ORDER BY d.end_date, d.id', p)
    today = date.today()
    out, links = [], []
    for d in rr:
        left = None
        try:
            left = (datetime.strptime(d['end_date'], '%Y-%m-%d').date() - today).days
        except (ValueError, TypeError):
            pass
        days = None
        try:
            days = (datetime.strptime(d['end_date'], '%Y-%m-%d').date()
                    - datetime.strptime(d['start_date'], '%Y-%m-%d').date()).days
        except (ValueError, TypeError):
            pass
        interest = (d['principal'] * d['rate'] * days / 365) if days else 0
        out.append({
            'company': d['company_name'], 'bank': d['bank'],
            'order_no': d['order_no'], 'currency': d['currency'],
            'principal': money(d['principal']), 'rate': d['rate'],
            'start_date': d['start_date'], 'end_date': d['end_date'],
            'days': days, 'interest_est': money(interest),
            'days_to_maturity': left,
            'matured': (left is not None and left < 0),
        })
        target = f'deposits:{d["company_id"]}'
        if len(links) < LINK_PER_TOOL and all(x['target'] != target for x in links):
            links.append({'label': f"{d['company_name']} · {d['bank']} "
                                   f"{money(d['principal']):,.2f} {d['currency']}",
                          'target': target})
    return {'deposits': out, 'count': len(out)}, links


def t_list_tags(args):
    rr = rows('SELECT t.id, t.name, t.color, COUNT(et.entry_id) n FROM tags t '
              'LEFT JOIN entry_tags et ON et.tag_id=t.id GROUP BY t.id')
    return {'tags': rr}, []


def t_list_cash_counts(args):
    cond, p = [], []
    if args.get('company_id'):
        cond.append('cc.company_id=?')
        p.append(int(args['company_id']))
    where = ('WHERE ' + ' AND '.join(cond)) if cond else ''
    rr = rows(f'SELECT cc.*, c.name company_name FROM cash_counts cc '
              f'JOIN companies c ON c.id=cc.company_id {where} '
              f'ORDER BY cc.count_date DESC, cc.id DESC LIMIT 50', p)
    links = []
    for r in rr:
        target = f"cash:{r['company_id']}"
        if len(links) < LINK_PER_TOOL and all(x['target'] != target for x in links):
            links.append({'label': f"{r['company_name']} · {r['count_date']} 盘点单",
                          'target': target})
    return {'cash_counts': rr, 'count': len(rr)}, links


def t_show_links(args):
    out = []
    for l in (args.get('links') or [])[:LINK_MODEL_MAX]:
        if not isinstance(l, dict):
            continue
        target = str(l.get('target') or '').strip()
        label = str(l.get('label') or target).strip()
        if not target:
            continue
        out.append({'label': label[:120], 'target': target})
    return {'ok': True, 'pushed': len(out)}, out


TOOLS = [
    {'type': 'function', 'function': {
        'name': 'list_companies',
        'description': '列出所有公司及其 id。查任何数据前先调用它确认公司。',
        'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    {'type': 'function', 'function': {
        'name': 'list_accounts',
        'description': '列出账户（含 id、所属公司、币种、银行、期初与当前余额）。'
                       '查流水、查余额前先拿 account_id。',
        'parameters': {'type': 'object', 'properties': {
            'company_id': {'type': 'integer', 'description': '只列该公司的账户，可省'}},
            'required': []}}},
    {'type': 'function', 'function': {
        'name': 'query_entries',
        'description': '查询日记账流水。支持按公司/账户/日期区间/关键词/对方单位/'
                       '项目/标签/收支方向/金额下限/兑现状态过滤，自动排序并返回合计。'
                       '这是最常用的工具。',
        'parameters': {'type': 'object', 'properties': {
            'company_id': {'type': 'integer'},
            'account_id': {'type': 'integer'},
            'date_from': {'type': 'string', 'description': 'YYYY-MM-DD'},
            'date_to': {'type': 'string', 'description': 'YYYY-MM-DD'},
            'keyword': {'type': 'string', 'description': '摘要/单据号/对方/项目/备注 模糊匹配'},
            'party': {'type': 'string', 'description': '对方单位模糊匹配'},
            'project': {'type': 'string', 'description': '项目模糊匹配'},
            'tag': {'type': 'string', 'description': '标签名模糊匹配'},
            'direction': {'type': 'string', 'enum': ['in', 'out'],
                          'description': 'in=只看收入，out=只看支出'},
            'min_amount': {'type': 'number', 'description': '只看金额≥此值的流水'},
            'status': {'type': 'string', 'enum': ['all', 'booked', 'pending'],
                       'description': 'booked=已入账，pending=未兑现/未到账'},
            'limit': {'type': 'integer', 'description': '返回条数，默认30，最大200'},
            'order': {'type': 'string', 'enum': ['desc', 'asc'], 'description': '默认 desc（最新在前）'}},
            'required': []}}},
    {'type': 'function', 'function': {
        'name': 'account_balance',
        'description': '查单个账户在某个日期的余额（账面余额 + 剔除未兑现/未到账后的实际余额）。',
        'parameters': {'type': 'object', 'properties': {
            'account_id': {'type': 'integer'},
            'as_of': {'type': 'string', 'description': 'YYYY-MM-DD，省略=今天'}},
            'required': ['account_id']}}},
    {'type': 'function', 'function': {
        'name': 'company_summary',
        'description': '按公司汇总：各币种/银行现金的余额，以及期间内的收支合计与笔数。'
                       '适合「这个月收支多少」「各公司现在有多少钱」这类问题。',
        'parameters': {'type': 'object', 'properties': {
            'company_id': {'type': 'integer', 'description': '省略=全部公司'},
            'date_from': {'type': 'string', 'description': 'YYYY-MM-DD'},
            'date_to': {'type': 'string', 'description': 'YYYY-MM-DD'}},
            'required': []}}},
    {'type': 'function', 'function': {
        'name': 'list_deposits',
        'description': '查定期存款：本金、利率、起止日、到期天数、估算利息、是否已到期。',
        'parameters': {'type': 'object', 'properties': {
            'company_id': {'type': 'integer'}}, 'required': []}}},
    {'type': 'function', 'function': {
        'name': 'list_tags',
        'description': '列出全部标签及每个标签下的流水笔数（未收收据、未签字等跟进事项）。',
        'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    {'type': 'function', 'function': {
        'name': 'list_cash_counts',
        'description': '查库存现金盘点单记录。',
        'parameters': {'type': 'object', 'properties': {
            'company_id': {'type': 'integer'}}, 'required': []}}},
    {'type': 'function', 'function': {
        'name': 'show_links',
        'description': '把链接作为独立按钮推给用户。仅在用户明确要「给我链接清单 / 列个链接」时使用；'
                       '正常情况下应把链接写在回答文字里（[文字](#entry:流水id:账户id)），不必调用本工具。'
                       'target 格式：journal:账户id、entry:流水id:账户id、'
                       'dashboard:公司id、deposits:公司id、cash:公司id、url:https://...；'
                       'query_entries 返回的每一行带 link 字段，可直接粘到 target。',
        'parameters': {'type': 'object', 'properties': {
            'links': {'type': 'array', 'items': {
                'type': 'object',
                'properties': {'label': {'type': 'string'},
                               'target': {'type': 'string'}},
                'required': ['label', 'target']}}},
            'required': ['links']}}},
]

TOOL_IMPL = {
    'list_companies': t_list_companies,
    'list_accounts': t_list_accounts,
    'query_entries': t_query_entries,
    'account_balance': t_account_balance,
    'company_summary': t_company_summary,
    'list_deposits': t_list_deposits,
    'list_tags': t_list_tags,
    'list_cash_counts': t_list_cash_counts,
    'show_links': t_show_links,
}

SYSTEM_PROMPT = """你是「日记账 BI 系统」内置的查账助手，服务对象是企业的财务人员。

## 硬性规则
1. **所有数字必须来自工具查询结果，绝不凭印象编造**。用户问及任何金额、笔数、账户、余额、流水时，先调工具。
2. 不知道是哪个账户/公司时，先用 list_companies / list_accounts 确认 id，再查数据。
3. 口径：
   - 收入 = debit（借方），支出 = credit（贷方），net = 收入 − 支出；
   - 账面余额 = 期初 + 收入 − 支出；
   - pending=1 表示「未兑现 / 未到账」，**实际可用余额要剔除它**；回答时要区分这两个口径。
   - 不同币种不可直接相加，除非明确按汇率折算（折算需说明用的汇率）。
4. 工具返回 `total` 与 `returned` 不一致时，要说明「共 X 笔，此处列出最新的 Y 笔」。
5. 不要暴露 entry_id、account_id 这类内部 id 给用户；流水行里的 `link` 字段只用来写链接，别把原始值写进正文。
6. **链接必须长在文字上**：回答里点到具体某笔流水 / 某个账户 / 某家公司时，把那段业务描述本身写成链接：
   `[张权破痛油 9月水电费 13,601.16](#entry:2087:4)`
   - target 直接抄工具返回的 `link` 字段（流水）、`journal:账户id`（账户）、`dashboard:公司id`（公司），**原样复制，绝不自己编**。
   - 表格里也这么写：`| 10-29 | [张权破痛油 水电费](#entry:2087:4) | 13,601.16 |`。
   - 只给回答里真正提到的那几条（最多 5 条）；查询结果里没提到的记录不要加链接。
   - 链接文字写业务描述（日期 / 对方 / 金额），**不要**把 `#entry:...` 露在正文里。
   - 纯汇总问题（本月收支、各币种余额、有几家公司）不需要任何链接。
   - 用了这种写法就**不要**再调 show_links。

## 回答风格
- 中文，简洁。**第一句先给结论**（例如「8 月支出合计 1,284,300.00 MOP，比 7 月多 12%」）。
- 明细用 Markdown 表格（不超过 10 行，多了先汇总），数字右对齐、两位小数、带千分位和币种。
- 金额较大或异常时，主动点出风险（如某笔占当月支出 40%、有未兑现支票、定期即将到期）。
- 结尾可给一句可执行的建议，但不要长篇大论。

今天是 {today}。当前系统默认公司：{company}。
"""


# ---------------------------------------------------------------- LLM 调用
def _endpoint(base_url):
    u = (base_url or '').strip().rstrip('/')
    if not u:
        raise ValueError('未配置 API 地址（Base URL）')
    if u.endswith('/chat/completions'):
        return u
    return u + '/chat/completions'


def llm_chat(cfg, messages, tools=None):
    payload = {
        'model': cfg['model'],
        'messages': messages,
        'temperature': float(cfg.get('temperature') or 0),
        'stream': False,
    }
    if tools:
        payload['tools'] = tools
        payload['tool_choice'] = 'auto'
    headers = {'Content-Type': 'application/json'}
    key = (cfg.get('api_key') or '').strip()
    if key:
        headers['Authorization'] = 'Bearer ' + key
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(_endpoint(cfg.get('base_url')),
                                 data=body, headers=headers, method='POST')
    timeout = max(10, int(cfg.get('timeout') or 90))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'replace')[:600]
        except Exception:
            pass
        msg = f'接口返回 HTTP {e.code}'
        try:
            j = json.loads(detail)
            em = (j.get('error') or {}).get('message')
            if em:
                msg += f'：{em}'
        except ValueError:
            if detail:
                msg += f'：{detail}'
        if e.code in (401, 403):
            msg += '（API Key 可能无效或无权限）'
        elif e.code == 404:
            msg += '（Base URL 或模型名可能不对，注意有的服务要带 /v1）'
        elif e.code == 429:
            msg += '（限流或余额不足）'
        raise RuntimeError(msg)
    except urllib.error.URLError as e:
        raise RuntimeError(f'连不上模型服务：{e.reason}。检查 Base URL、网络/代理，'
                           f'本地 Ollama 需先启动。')
    except TimeoutError:
        raise RuntimeError(f'模型响应超时（>{timeout}s），可换更快的模型或调大超时。')
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError('接口返回的不是 JSON，可能 Base URL 指向了网页而不是 API。')
    if isinstance(data, dict) and data.get('error'):
        raise RuntimeError(str(data['error'].get('message') or data['error']))
    return data


def _msg_of(data):
    choices = data.get('choices') or []
    if not choices:
        raise RuntimeError('模型没有返回任何内容（choices 为空）')
    return choices[0].get('message') or {}


# ---------------------------------------------------------------- Agent 循环
def run_agent(cfg, history, user_text):
    """history: [{role, content}]（不含本次），返回 {reply, links, steps, usage}"""
    try:
        con = db()
        comp = con.execute('SELECT name FROM companies ORDER BY sort, id LIMIT 1'
                           ).fetchone()
        con.close()
        company_name = comp['name'] if comp else '（未设置）'
    except sqlite3.Error:
        company_name = '（未设置）'

    messages = [{'role': 'system', 'content': SYSTEM_PROMPT.format(
        today=date.today().isoformat(), company=company_name)}]
    for h in (history or [])[-12:]:
        role = h.get('role')
        if role in ('user', 'assistant') and h.get('content'):
            messages.append({'role': role, 'content': str(h['content'])[:4000]})
    messages.append({'role': 'user', 'content': user_text})

    auto_links, pushed_links, steps = [], [], []
    usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
    rounds = max(1, min(int(cfg.get('max_rounds') or 6), 12))
    reply = ''
    for _ in range(rounds):
        data = llm_chat(cfg, messages, TOOLS)
        u = data.get('usage') or {}
        for k in usage:
            usage[k] += int(u.get(k) or 0)
        msg = _msg_of(data)
        calls = msg.get('tool_calls') or []
        if not calls:
            reply = (msg.get('content') or '').strip()
            break
        # 把模型的工具调用意图原样回填
        messages.append({'role': 'assistant',
                         'content': msg.get('content') or '',
                         'tool_calls': calls})
        for tc in calls:
            fn = (tc.get('function') or {})
            name = fn.get('name') or ''
            raw_args = fn.get('arguments') or '{}'
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except ValueError:
                args = {}
            impl = TOOL_IMPL.get(name)
            t0 = datetime.now()
            if not impl:
                result, lk = {'error': f'未知工具 {name}'}, []
            else:
                try:
                    result, lk = impl(args if isinstance(args, dict) else {})
                except Exception as exc:  # 工具异常不能让整轮崩掉
                    result, lk = {'error': f'{type(exc).__name__}: {exc}'}, []
            # 模型主动推送的链接优先级最高（那才是「和回答对得上」的），
            # 自动收集的只在模型没推时兜底。
            bucket = pushed_links if name == 'show_links' else auto_links
            for l in lk:
                if not isinstance(l, dict):
                    continue
                tgt = str(l.get('target') or '').strip()
                if not tgt or any(x['target'] == tgt for x in bucket):
                    continue
                bucket.append({'label': str(l.get('label') or tgt)[:120],
                               'target': tgt})
            steps.append({
                'tool': name, 'args': args,
                'ms': int((datetime.now() - t0).total_seconds() * 1000),
                'summary': _summarize(name, result),
            })
            messages.append({'role': 'tool', 'tool_call_id': tc.get('id') or name,
                             'content': json.dumps(result, ensure_ascii=False)[:16000]})
    else:
        reply = reply or ('已达到工具调用上限仍未得出结论。可以把问题拆小一点，'
                          '例如先问「有哪些公司」，再问具体某家某月的数据。')

    if not reply:
        reply = '（模型没有返回文字内容。可点「重新生成」，或换一个模型试试。）'
    # 回答正文里已经内联了链接，就不再在底部重复一堆按钮
    inline = bool(re.search(r'\]\(#[A-Za-z]+:', reply))
    shown = [] if inline else (pushed_links or auto_links)
    return {'reply': reply, 'links': shown[:LINK_TOTAL], 'inline': inline,
            'pushed': bool(pushed_links), 'steps': steps, 'usage': usage}


def _summarize(name, result):
    if not isinstance(result, dict):
        return ''
    if result.get('error'):
        return '出错：' + str(result['error'])[:80]
    if name == 'query_entries':
        return f"共 {result.get('total', 0)} 笔，返回 {result.get('returned', 0)} 笔，" \
               f"收 {result.get('sum_in', 0):,.2f} / 支 {result.get('sum_out', 0):,.2f}"
    if name == 'list_accounts':
        return f"{result.get('count', 0)} 个账户"
    if name == 'list_companies':
        return f"{result.get('count', 0)} 家公司"
    if name == 'company_summary':
        return f"{len(result.get('summaries') or [])} 家公司的汇总"
    if name == 'list_deposits':
        return f"{result.get('count', 0)} 笔定期"
    if name == 'show_links':
        return f"推送 {result.get('pushed', 0)} 个链接"
    if name == 'account_balance':
        return f"实际余额 {result.get('actual_balance', 0):,.2f}" if 'actual_balance' in result else ''
    return ''


# ---------------------------------------------------------------- 路由
bp = Blueprint('agent', __name__)


@bp.get('/api/agent/config')
def get_agent_config():
    cfg = load_config()
    safe = dict(cfg)
    safe['api_key'] = mask_key(cfg.get('api_key') or '')
    safe['has_key'] = bool((cfg.get('api_key') or '').strip())
    return jsonify({'config': safe, 'presets': PRESETS})


@bp.post('/api/agent/config')
def set_agent_config():
    data = request.get_json(force=True) or {}
    cfg = load_config()
    for k in ('provider', 'base_url', 'model'):
        if k in data:
            cfg[k] = str(data.get(k) or '').strip()
    if 'api_key' in data:
        k = str(data.get('api_key') or '')
        # 前端回传掩码值＝不修改
        if '****' not in k:
            cfg['api_key'] = k.strip()
    for k, lo, hi in (('temperature', 0, 2), ('max_rounds', 1, 12),
                      ('timeout', 10, 300)):
        if k in data:
            try:
                cfg[k] = max(lo, min(hi, float(data[k])))
            except (TypeError, ValueError):
                pass
    if not cfg.get('base_url'):
        return jsonify({'error': 'Base URL 不能为空'}), 400
    if not cfg.get('model'):
        return jsonify({'error': '模型名不能为空'}), 400
    save_config(cfg)
    safe = dict(cfg)
    safe['api_key'] = mask_key(cfg.get('api_key') or '')
    safe['has_key'] = bool((cfg.get('api_key') or '').strip())
    return jsonify({'ok': True, 'config': safe})


@bp.post('/api/agent/test')
def test_agent():
    cfg = load_config()
    data = request.get_json(silent=True) or {}
    # 允许用未保存的表单值直接测试
    for k in ('base_url', 'model'):
        if data.get(k):
            cfg[k] = str(data[k]).strip()
    k = str(data.get('api_key') or '')
    if k and '****' not in k:
        cfg['api_key'] = k.strip()
    cfg['timeout'] = 25
    try:
        data = llm_chat(cfg, [
            {'role': 'user', 'content': '只回复两个字：可用'}], None)
        msg = _msg_of(data)
        text = (msg.get('content') or '').strip()[:40]
        return jsonify({'ok': True, 'model': cfg['model'], 'reply': text})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 200


@bp.post('/api/agent/chat')
def agent_chat():
    data = request.get_json(force=True) or {}
    text = str(data.get('message') or '').strip()
    if not text:
        return jsonify({'error': '请输入问题'}), 400
    cfg = load_config()
    if not cfg.get('base_url') or not cfg.get('model'):
        return jsonify({'error': '还没配置模型，请到「设置 → AI 模型」填写 Base URL 和模型名'}), 400
    if not (cfg.get('api_key') or '').strip() and 'localhost' not in cfg['base_url'] \
            and '127.0.0.1' not in cfg['base_url']:
        return jsonify({'error': '还没填 API Key，请到「设置 → AI 模型」填写'}), 400
    try:
        out = run_agent(cfg, data.get('history') or [], text)
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 200
    except Exception as exc:
        return jsonify({'error': f'{type(exc).__name__}: {exc}'}), 200
    out['model'] = cfg.get('model')
    return jsonify(out)
