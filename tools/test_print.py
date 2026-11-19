# -*- coding: utf-8 -*-
"""票据打印并入后的接口自测（副本数据，不碰真实库）。"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(tempfile.gettempdir(), 'jbtest_print')
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
import print_tpl as P  # noqa: E402

c = A.app.test_client()
ok = fail = 0


def chk(name, cond, extra=''):
    global ok, fail
    if cond:
        ok += 1
        print('  ok   ' + name)
    else:
        fail += 1
        print('  FAIL ' + name + ('  ' + str(extra) if extra else ''))


print('== 金额大写（与前端同算法）==')
for raw, want in [
    ('564,326.40', '伍拾陆万肆仟叁佰贰拾陆元肆角整'),
    ('491500.00', '肆拾玖万壹仟伍佰元整'),
    ('10000', '壹万元整'),
    ('0.05', '伍分'),
    ('100000001', '壹亿零壹元整'),
    ('1008', '壹仟零捌元整'),
    ('1,180,000.00', '壹佰壹拾捌万元整'),
]:
    got = P.to_chinese_amount(raw)
    chk('%s → %s' % (raw, want), got == want, got)
chk('toFigureAmount', P.to_figure_amount('MOP 564326.4') == '564,326.40')
chk('toDigitsUpper', P.to_digits_upper('5316.92') == '伍叁壹陆玖贰')

print('== 种子 ==')
r = c.get('/api/print/templates')
d = r.get_json()
chk('模板列表 200', r.status_code == 200)
chk('灌入 5 套内置模板', len(d['items']) == 5, [x['name'] for x in d['items']])
names = [x['name'] for x in d['items']]
print('  模板:', names)
tpls = {x['name']: x for x in d['items']}
chk('中银 45 字段', tpls.get('中银汇款单(A4)', {}).get('field_count') == 45)
chk('汇款申请书 20 字段', tpls.get('汇款申请书(A4)', {}).get('field_count') == 20)
chk('底图走 assets 前缀', all(x['bg'].startswith('assets/') for x in d['items']))

mop = tpls['默认支票']['id']
r = c.get('/api/print/templates/' + mop)
t = r.get_json()
chk('取单个模板', r.status_code == 200 and t['name'] == '默认支票')
chk('字段带 value', 'value' in t['fields'][0])
chk('坐标保留', abs(t['fields'][0]['x'] - 25.5) < 0.01, t['fields'][0]['x'])

print('== 底图 ==')
r = c.get('/api/print/bg/assets/mop.jpg')
chk('内置底图 200', r.status_code == 200 and len(r.data) > 50000, len(r.data))
r = c.get('/api/print/bg/../app.py')
chk('越界底图 404', r.status_code == 404, r.status_code)

print('== 填值 + 自动大写 ==')
r = c.post('/api/print/templates/%s/values' % mop, json={
    'values': {'存根·金额': 'MOP 564,326.40', '存根·收款人': '测试收款人', '支票·日期': '2026/11/19'}
})
d = r.get_json()
chk('set_values 200', r.status_code == 200, d)
chk('摘要值写入', d['values'].get('cf_payee') == '测试收款人')
chk('自动大写跟着变', d['values'].get('ch_words') == '伍拾陆万肆仟叁佰贰拾陆元肆角整', d['values'].get('ch_words'))
chk('自动小写', d['values'].get('ch_figs') == '564,326.40', d['values'].get('ch_figs'))

r = c.post('/api/print/templates/%s/values' % mop, json={'values': {'不存在的字段': 'x'}})
d = r.get_json()
chk('未知字段给提示', d.get('unknown') == ['不存在的字段'], d)

print('== 记录 CRUD ==')
r = c.post('/api/print/templates/%s/records' % mop, json={'name': '第二张', 'values': {'支票·收款人': '甲'}})
d = r.get_json()
chk('新增记录', r.status_code == 200 and d['index'] == 1, d)
r = c.put('/api/print/templates/%s/records/1' % mop, json={'name': '改过名', 'values': {'支票·收款人': '乙'}})
chk('改记录', r.get_json().get('name') == '改过名')
r = c.delete('/api/print/templates/%s/records/1' % mop)
chk('删记录', r.status_code == 200)
r = c.delete('/api/print/templates/%s/records/0' % mop)
chk('只剩一条时拒绝删记录', r.status_code == 400)

print('== 字段 CRUD ==')
r = c.post('/api/print/templates/%s/fields' % mop, json={'name': '新字段', 'x': 10, 'y': 12, 'size': 3})
f = r.get_json()
chk('新增字段', r.status_code == 200 and f['x'] == 10)
r = c.patch('/api/print/templates/%s/fields/%s' % (mop, f['id']), json={'x': 33.5, 'size': 4.2})
chk('改坐标', r.get_json()['field']['x'] == 33.5)
r = c.patch('/api/print/templates/%s/fields/%s' % (mop, 'nope'), json={'x': 1})
chk('未知字段 404', r.status_code == 404)
r = c.delete('/api/print/templates/%s/fields/%s' % (mop, f['id']))
chk('删字段', r.status_code == 200)

print('== 模板 CRUD ==')
r = c.post('/api/print/templates', json={'name': '自建模板', 'settings': {'w': 210, 'h': 297, 'rot': '0'}})
nt = r.get_json()
chk('新建模板', r.status_code == 200 and nt['settings']['rot'] == '0', nt.get('settings'))
r = c.put('/api/print/templates/' + nt['id'], json={'name': '自建模板2', 'fields': [], 'records': [{'name': 'a', 'values': {}}], 'settings': {'w': 210, 'h': 297}})
chk('整份保存', r.get_json()['name'] == '自建模板2')
r = c.delete('/api/print/templates/' + nt['id'])
chk('删除模板', r.status_code == 200)
r = c.get('/api/print/templates/not-exist')
chk('取不存在 404', r.status_code == 404)

print('== 导入（旧版 localStorage 导出格式）==')
legacy = {'v': 2, 'templates': [{
    'id': 'old1', 'name': '旧工具模板', 'bg': '',
    'fields': [{'id': 'a1', 'name': '收款人', 'x': 12, 'y': 34, 'size': 3}],
    'records': [{'name': 'r1', 'values': {'a1': '旧值'}}],
    'active': 0, 'settings': {'w': 275, 'h': 90, 'rot': '90cw', 'showbg': True}
}]}
r = c.post('/api/print/import', json=legacy)
d = r.get_json()
chk('导入 1 套', r.status_code == 200 and d['count'] == 1, d)
r = c.get('/api/print/templates')
chk('导入后 6 套', len(r.get_json()['items']) == 6)
tid = [x for x in r.get_json()['items'] if x['name'] == '旧工具模板'][0]['id']
t2 = c.get('/api/print/templates/' + tid).get_json()
chk('坐标迁移正确', abs(t2['fields'][0]['x'] - 12) < .01 and abs(t2['fields'][0]['y'] - 34) < .01)
r = c.post('/api/print/import', json=legacy)
r2 = c.get('/api/print/templates')
chk('同名再导入不重复', len(r2.get_json()['items']) == 6, len(r2.get_json()['items']))

print('== 底图上传 ==')
import base64
png1x1 = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
r = c.post('/api/print/bg', json={'dataurl': 'data:image/png;base64,' + png1x1})
d = r.get_json()
chk('dataurl 上传', r.status_code == 200 and d['bg'].startswith('file/'), d)
r = c.get('/api/print/bg/' + d['bg'])
chk('上传的底图可读回', r.status_code == 200)
r = c.post('/api/print/bg', json={'dataurl': 'nope'})
chk('非法 dataurl 400', r.status_code == 400)

print('\n结果: %d 通过, %d 失败' % (ok, fail))
sys.exit(1 if fail else 0)
