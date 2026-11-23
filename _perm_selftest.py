# -*- coding: utf-8 -*-
"""隔离环境下的权限与工具自测（不碰真实账本）"""
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix='biperm_')
shutil.copy(os.path.join(BASE, 'data', 'journal.db'), os.path.join(TMP, 'journal.db'))
os.makedirs(os.path.join(TMP, 'uploads'), exist_ok=True)
# 临时目录里没有 agent_perm.json 时会落到默认的 ask 模式，后面「直写」断言必然失败。
# 这份自测本来就是要验 full（全部放行）下的闸门，所以先把模式写进隔离环境。
with open(os.path.join(TMP, 'agent_perm.json'), 'w', encoding='utf-8') as f:
    json.dump({'mode': 'full'}, f)
os.environ['JOURNAL_DATA_DIR'] = TMP
os.environ['NO_BROWSER'] = '1'
sys.path.insert(0, BASE)

import app as A          # noqa: E402
import agent_core as C   # noqa: E402

OUT = []


def call(name, args, sid='test-sid'):
    res, links, ms = C.run_tool(name, args, sid)
    OUT.append((name, args, res))
    print(f"\n>>> {name} {json.dumps(args, ensure_ascii=False)[:120]}")
    print('   ', json.dumps(res, ensure_ascii=False, default=str)[:600])
    return res


with A.app.test_request_context('/'):
    print('数据目录:', TMP)
    r = call('system_info', {})
    assert r.get('companies'), 'system_info 没返回公司'

    # 1. 建公司
    r = call('manage_company', {'action': 'create', 'name': '测试权限公司'})
    assert r.get('ok'), r
    co_id = r['id']

    # 2. 建账户
    r = call('manage_account', {'action': 'create', 'company_id': co_id,
                                'name': '测试银行户', 'currency': 'MOP',
                                'bank': '测试银行', 'opening': 100000})
    assert r.get('ok'), r
    acc_id = r['id']

    # 3. 选一个真实账户做流水测试
    real = A.get_db().execute(
        'SELECT id, name FROM accounts WHERE id<>? ORDER BY id LIMIT 1', (acc_id,)).fetchone()
    print('\n真实账户:', dict(real) if real else None)

    # 4. 直写：记一笔（策略默认 create=auto）
    r = call('propose_entry', {'account_id': acc_id, 'date': '2026-11-17',
                               'amount': 1234.56, 'direction': 'out',
                               'description': '权限测试 材料款',
                               'party': '测试供应商'})
    assert r.get('auto') is True, f'未直写：{r}'
    wid = r.get('wid')
    eid = (r.get('written') or [{}])[0].get('id')
    assert eid, '没拿到写入的流水 id'

    # 5. 查得到
    r = call('query_entries', {'account_id': acc_id, 'keyword': '权限测试'})
    assert r.get('total') == 1, f'查不到刚写的流水：{r}'

    # 6. 撤销
    r = call('undo_write', {'wid': wid})
    assert r.get('ok'), r
    r = call('query_entries', {'account_id': acc_id, 'keyword': '权限测试'})
    assert r.get('total') == 0, f'撤销后还在：{r}'

    # 7. 修改也应直写
    r = call('propose_entry', {'account_id': acc_id, 'date': '2026-11-16',
                               'amount': 500, 'direction': 'in',
                               'description': '权限测试 回款'})
    eid2 = (r.get('written') or [{}])[0].get('id')
    r = call('propose_update_entry', {'entry_id': eid2, 'amount': 666.66})
    assert r.get('auto') is True, f'修改没直写：{r}'
    row = A.get_db().execute('SELECT debit,credit FROM entries WHERE id=?', (eid2,)).fetchone()
    print('\n修改后金额:', dict(row))

    # 8. 删除应保持 confirm（不直写）
    r = call('propose_delete_entries', {'entry_ids': [eid2], 'reason': '测试'})
    assert not r.get('auto'), f'删除不该直写：{r}'
    assert r.get('pid'), '删除应生成草稿'

    # 9. 标签
    r = call('manage_tag', {'action': 'create', 'name': '权限测试标签', 'color': '#ef4444'})
    assert r.get('ok'), r
    r = call('manage_tag', {'action': 'delete', 'tag': '权限测试标签'})
    assert r.get('need_confirm'), f'删标签应要确认：{r}'
    r = call('manage_tag', {'action': 'delete', 'tag': '权限测试标签', 'confirm': True})
    assert r.get('ok'), r

    # 10. 汇率
    r = call('set_exchange_rates', {'rates': {'HKD': 1.03, 'CNY': 1.13}})
    assert r.get('ok'), r

    # 11. 定期
    r = call('manage_deposit', {'action': 'create', 'company_id': co_id,
                                'bank': '测试银行', 'currency': 'HKD',
                                'principal': 500000, 'rate': 2.15,
                                'start_date': '2026-01-01', 'end_date': '2026-12-31'})
    assert r.get('ok'), r
    dep = r['id']
    r = call('list_deposits', {'company_id': co_id})
    assert r.get('deposits'), '查不到定期'

    # 12. 删公司要确认
    r = call('manage_company', {'action': 'delete', 'company_id': co_id})
    assert r.get('need_confirm'), f'删公司应要确认：{r}'
    r = call('manage_company', {'action': 'delete', 'company_id': co_id, 'confirm': True})
    assert r.get('ok'), r

    # 13. 备份
    r = call('backup_now', {})
    assert r.get('ok'), r
    print('\n备份文件存在:', os.path.exists(r['file']))

print('\n================ 全部断言通过 ================')
shutil.rmtree(TMP, ignore_errors=True)
