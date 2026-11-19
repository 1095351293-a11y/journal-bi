# -*- coding: utf-8 -*-
"""Agent 票据打印工具自测（副本数据 + 副本模板目录）。"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(tempfile.gettempdir(), 'jbtest_agentprint')
if os.path.isdir(TMP):
    shutil.rmtree(TMP)
os.makedirs(TMP)
SRC = os.path.join(ROOT, 'data', 'journal.db')
if os.path.isfile(SRC):
    shutil.copyfile(SRC, os.path.join(TMP, 'journal.db'))
os.environ['JOURNAL_DATA_DIR'] = TMP
os.environ['NO_BROWSER'] = '1'
sys.path.insert(0, ROOT)

import app as A  # noqa: E402
import agent_print as AP  # noqa: E402
import print_tpl as PT  # noqa: E402

ok = fail = 0


def chk(name, cond, extra=''):
    global ok, fail
    if cond:
        ok += 1
        print('  ok   ' + name)
    else:
        fail += 1
        print('  FAIL ' + name + ('  ' + str(extra) if extra else ''))


def run(name, args=None):
    fn = AP.IMPL[name]
    r = fn(args or {})
    assert isinstance(r, tuple) and len(r) == 2, (name, type(r))
    return r[0]


# 权限模式：先设成 full，测试真实写入；再测 ask 出卡
import agent_perm  # noqa: E402
agent_perm.set_mode('full')

print('== 工具注册 ==')
from agent import TOOLS, TOOL_IMPL, WRITE_NAMES  # noqa: E402
names = [t['function']['name'] for t in TOOLS]
print_tools = [n for n in names if 'print' in n]
chk('9 个打印工具全部注册', len(print_tools) == 9, print_tools)
chk('都在 TOOL_IMPL', all(n in TOOL_IMPL for n in print_tools))
chk('写工具进 WRITE_NAMES', all(n in WRITE_NAMES for n in (
    'create_print_record', 'update_print_record', 'delete_print_record',
    'update_print_field', 'create_print_template', 'delete_print_template')))
chk('读工具不在 WRITE_NAMES', 'list_print_templates' not in WRITE_NAMES)

print('== 读 ==')
r = run('list_print_templates')
chk('列模板 5 套', r.get('count') == 5, r.get('count'))
r = run('get_print_template', {'template': '中银'})
chk('模糊匹配模板名', r.get('name') == '中银汇款单(A4)', r.get('name'))
chk('字段 45 个', len(r.get('fields') or []) == 45)
r2 = run('get_print_template', {'template': '默认支票'})
chk('标出金额来源字段', bool(r2.get('auto_amount_field')), r2.get('auto_amount_field'))
r = run('get_print_record', {'template': '默认支票'})
chk('取当前票据', 'values' in r and r.get('link', '').startswith('#print/'), r.get('link'))

print('== 从流水开票 ==')
# 找一笔支出流水
import sqlite3
con = sqlite3.connect(os.path.join(TMP, 'journal.db'))
con.row_factory = sqlite3.Row
e = con.execute('SELECT id, date, party, description, credit, account_id FROM entries '
                'WHERE credit > 1000 ORDER BY id DESC LIMIT 1').fetchone()
con.close()
chk('找到测试流水', e is not None)
e = dict(e)
print('  流水 #%s %s %s %.2f' % (e['id'], e['date'], e['party'], e['credit']))

r = run('create_print_record', {'template': '中银汇款单', 'from_entry_id': e['id']})
chk('开票成功', bool(r.get('ok')), r.get('error'))
chk('返回链接', str(r.get('link', '')).startswith('#print/'), r.get('link'))
vals = r.get('values') or {}
chk('金额带进金额字段', any(str(v).replace(',', '').find('%.2f' % e['credit']) >= 0
                            for v in vals.values()), [v for v in vals.values() if v][:5])
chk('日期带进日期字段', any(str(e['date']).replace('-', '/') in str(v) for v in vals.values()))
chk('列出待补字段', isinstance(r.get('need_input'), list), r.get('need_input'))

print('== 手填 + 自动大写 ==')
r = run('create_print_record', {'template': '默认支票', 'name': '测试支票',
                                'values': {'存根·金额': 'MOP 12345.67', '支票·收款人': '张三'}})
vals = r.get('values') or {}
chk('新建手填票', bool(r.get('ok')), r.get('error'))
chk('大写正确', vals.get('ch_words') == '壹万贰仟叁佰肆拾伍元陆角柒分', vals.get('ch_words'))
chk('小写正确', vals.get('ch_figs') == '12,345.67', vals.get('ch_figs'))
idx = r.get('index')

r = run('update_print_record', {'template': '默认支票', 'record': idx,
                                'values': {'存根·金额': 'HKD 1000'}})
chk('改金额后大写跟着变', (r.get('values') or {}).get('ch_words') == '壹仟元整',
    (r.get('values') or {}).get('ch_words'))
r = run('update_print_record', {'template': '默认支票', 'record': idx, 'values': {'乱写': 'x'}})
chk('未知字段报错', bool(r.get('error')), r)

print('== 坐标微调限幅 ==')
r = run('update_print_field', {'template': '默认支票', 'field': '存根·日期', 'x': 25.5})
before = r['before']['x']
r = run('update_print_field', {'template': '默认支票', 'field': '存根·日期', 'x': 25.5 + 50})
after = r['after']['x']
chk('单次最多挪 3mm', abs(after - before - 3.0) < 0.15, (before, after))
r = run('update_print_field', {'template': '默认支票', 'field': '不存在的字段', 'x': 1})
chk('字段不存在报错', bool(r.get('error')))

print('== 删除 ==')
r = run('delete_print_record', {'template': '默认支票', 'record': idx})
chk('删票据', bool(r.get('ok')), r.get('error'))
r = run('delete_print_record', {'template': '默认支票', 'record': 0})
chk('只剩一张时拒绝', bool(r.get('error')), r)
r = run('create_print_template', {'name': 'Agent 新模板', 'fields': [{'name': '收款人', 'x': 10, 'y': 20}]})
chk('新建模板', bool(r.get('ok')), r.get('error'))
r = run('delete_print_template', {'template': 'Agent 新模板'})
chk('删模板', bool(r.get('ok')), r.get('error'))

print('== 权限闸门（ask 模式出卡）==')
agent_perm.set_mode('ask')
r = run('create_print_record', {'template': '默认支票', 'values': {'支票·收款人': '李四'}})
chk('ask 模式生成审核卡', bool(r.get('pending_approval')), r)
chk('审核卡带 pid', bool(r.get('pid')))
chk('审核卡没真写', 'template' not in r or not r.get('ok'))
n_before = len([t for t in PT.load_all() if t['name'] == '默认支票'][0]['records'])
r2 = run('create_print_record', {'template': '默认支票', '__approved': True,
                                 'values': {'支票·收款人': '王五'}})
chk('审核通过后真写', bool(r2.get('ok')), r2.get('error'))
n_after = len([t for t in PT.load_all() if t['name'] == '默认支票'][0]['records'])
chk('记录数 +1', n_after == n_before + 1, (n_before, n_after))
r3 = run('create_print_record', {'template': '默认支票', 'confirm': True,
                                 'values': {'支票·收款人': '赵六'}})
chk('ask 模式下模型带 confirm 无效', bool(r3.get('pending_approval')), r3)

agent_perm.set_mode('auto')
r = run('update_print_field', {'template': '默认支票', 'field': '存根·日期', 'x': 25.5})
chk('auto 模式 update 直接写', bool(r.get('ok')), r)
r = run('delete_print_record', {'template': '默认支票', 'record': 0})
chk('auto 模式 delete 仍要确认', bool(r.get('pending_approval')), r)

print('== 只读模式不下发 ==')
agent_perm.set_mode('readonly')
import agent_core  # noqa: E402
tools_ro = agent_core.pick_tools([t['function']['name'] for t in TOOLS])
names_ro = [t['function']['name'] for t in tools_ro]
chk('只读无写工具', not any(n in names_ro for n in (
    'create_print_record', 'update_print_record', 'delete_print_record',
    'update_print_field', 'create_print_template', 'delete_print_template')),
    [n for n in names_ro if 'print' in n])
chk('只读保留查询工具', 'list_print_templates' in names_ro)

agent_perm.set_mode('ask')
print('\n结果: %d 通过, %d 失败' % (ok, fail))
sys.exit(1 if fail else 0)
