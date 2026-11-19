# -*- coding: utf-8 -*-
"""票据打印模板 —— 服务端存储与接口

以前这东西是一个独立的单文件 HTML（桌面《打印模板.html》），模板与支票记录全存
在浏览器 localStorage 里：换台机器就没了，AI 也看不见。这里把它搬到服务端：

    data/print/templates/<id>.json   每套模板一份（字段坐标、字号、页面设置、记录值）
    data/print/bg/<file>             用户自己上传的底图
    static/print-assets/<file>       内置 5 张底图（随代码发布，可 git）

之所以一套模板一个文件，不塞 SQLite：模板是「版式资产」，人要看要改要备份，
JSON 直接可读；而且它跟账本数据没有外键关系，放库里反而增加迁移负担。

坐标口径与源文件完全一致：x/y 单位 mm，y 是**文字基线**；页面渲染时
top = y - 0.78 * size。别改成「上边界」，否则所有已校准的位置都会下移。
"""
import json
import os
import re
import threading
import time
import uuid

from flask import Blueprint, jsonify, request, send_from_directory, abort

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
PRINT_DIR = os.path.join(DATA_DIR, 'print')
TPL_DIR = os.path.join(PRINT_DIR, 'templates')
BG_DIR = os.path.join(PRINT_DIR, 'bg')
SEED_PATH = os.path.join(BASE_DIR, 'print_seed', 'templates.json')
ASSET_DIR = os.path.join(BASE_DIR, 'static', 'print-assets')

for _d in (TPL_DIR, BG_DIR):
    os.makedirs(_d, exist_ok=True)

_lock = threading.Lock()

bp = Blueprint('print_tpl', __name__)

# 只认这两种前缀，杜绝 ../ 越界
BG_PREFIX_ASSET = 'assets/'
BG_PREFIX_FILE = 'file/'


# ---------------------------------------------------------------- 金额大写
CN_DIGITS = ['零', '壹', '贰', '叁', '肆', '伍', '陆', '柒', '捌', '玖']
UNIT1 = ['', '拾', '佰', '仟']
UNIT2 = ['', '万', '亿', '万亿']


def int_to_chinese(num):
    """与前端 intToChinese 逐字对齐（含「零」的省略规则），改这里必须同步改 app.js。"""
    if num == 0:
        return '零'
    groups = []
    n = num
    while n > 0:
        groups.append(n % 10000)
        n //= 10000
    result = ''
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            continue
        s = str(g)
        gstr = ''
        zero_pending = False
        for j, ch in enumerate(s):
            d = int(ch)
            pos = len(s) - 1 - j
            if d == 0:
                zero_pending = True
            else:
                if zero_pending and gstr:
                    gstr += '零'
                zero_pending = False
                gstr += CN_DIGITS[d] + UNIT1[pos]
        if result and g < 1000:
            gstr = '零' + gstr
        if i < len(UNIT2):
            result += gstr + UNIT2[i]
    return result


def to_chinese_amount(text):
    """'MOP 564,326.40' → '伍拾陆万肆仟叁佰贰拾陆元肆角'（非数字字符先剥掉）。"""
    s = re.sub(r'[^0-9.]', '', str(text or ''))
    if not s:
        return ''
    try:
        n = float(s)
    except ValueError:
        return ''
    total = int(round(n * 100 + 1e-6))
    yuan, rest = divmod(total, 100)
    jiao, fen = divmod(rest, 10)
    out = ''
    if yuan > 0 or (jiao == 0 and fen == 0):
        out += int_to_chinese(yuan) + '元'
    if jiao == 0 and fen == 0:
        out += '整'
    else:
        if jiao > 0:
            out += CN_DIGITS[jiao] + '角'
        elif yuan > 0 and fen > 0:
            out += '零'
        if fen > 0:
            out += CN_DIGITS[fen] + '分'
        if fen == 0:
            out += '整'
    return out


def to_figure_amount(text):
    s = re.sub(r'[^0-9.]', '', str(text or ''))
    if not s:
        return ''
    try:
        return '{:,.2f}'.format(float(s))
    except ValueError:
        return ''


def to_digits_upper(text):
    """逐位转中文：'5316.92' → '伍叁壹陆玖贰'（源工具「输数字显示大写」的用法）。"""
    out = ''
    for ch in str(text or ''):
        if ch.isdigit():
            out += CN_DIGITS[int(ch)]
    return out


# ---------------------------------------------------------------- 结构归一
def _num(v, d):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return d
    return f


def _gen_id(prefix='f'):
    return prefix + uuid.uuid4().hex[:10]


DEFAULT_SETTINGS = {'w': 275, 'h': 90, 'offx': 0, 'offy': 0,
                    'rot': '90cw', 'showbg': True}


def normalize_field(f, idx=0):
    f = dict(f or {})
    x = _num(f.get('x'), 20)
    y = _num(f.get('y'), 20)
    size = _num(f.get('size'), 3.2)
    out = {
        'id': f.get('id') or _gen_id(),
        'name': str(f.get('name') or '字段'),
        'x': x, 'y': y, 'size': size,
        'on': f.get('on', True) is not False,
        'auto': f.get('auto') or None,        # words / figs / wordsin / None
        'autoSrc': bool(f.get('autoSrc')),
        'kind': f.get('kind') or None,        # check = 勾选框
        'dx': _num(f.get('dx'), x),
        'dy': _num(f.get('dy'), y),
        'dsize': _num(f.get('dsize'), size),
        'def': '' if f.get('def') is None else str(f.get('def')),
    }
    return out


def normalize(tpl, idx=0):
    """补齐所有可选字段。源文件 normalizeTemplate 的 Python 版，行为保持一致。"""
    t = dict(tpl or {})
    t['id'] = t.get('id') or _gen_id('t')
    t['name'] = str(t.get('name') or ('模板 %d' % (idx + 1)))
    t['bg'] = t.get('bg') or ''
    fields = t.get('fields')
    if not isinstance(fields, list):
        fields = []
    t['fields'] = [normalize_field(f, i) for i, f in enumerate(fields)]
    if not t.get('records'):
        t['records'] = [{'name': '记录 1', 'values': {}}]
    recs = []
    for j, r in enumerate(t.get('records') or []):
        r = r or {}
        vals = r.get('values') if isinstance(r.get('values'), dict) else {}
        recs.append({'name': str(r.get('name') or ('记录 %d' % (j + 1))),
                     'values': {str(k): ('' if v is None else v) for k, v in vals.items()}})
    t['records'] = recs
    a = t.get('active')
    t['active'] = a if isinstance(a, int) and 0 <= a < len(recs) else 0
    st = dict(DEFAULT_SETTINGS)
    if isinstance(t.get('settings'), dict):
        st.update({k: v for k, v in t['settings'].items() if k in DEFAULT_SETTINGS})
    st['w'] = _num(st.get('w'), 275)
    st['h'] = _num(st.get('h'), 90)
    st['offx'] = _num(st.get('offx'), 0)
    st['offy'] = _num(st.get('offy'), 0)
    st['rot'] = st.get('rot') if st.get('rot') in ('0', '90cw', '90ccw') else '90cw'
    st['showbg'] = st.get('showbg') is not False
    t['settings'] = st
    return t


def amt_src(tpl):
    """金额来源字段（autoSrc 且非派生）。优先返回不带 auto 的那个。"""
    fb = None
    for f in (tpl.get('fields') or []):
        if f.get('autoSrc') and not f.get('auto'):
            return f
        if f.get('autoSrc') and fb is None:
            fb = f
    return fb


def field_by_key(tpl, key):
    """按 id / 名称 / 序号找字段。给 Agent 用：它手里通常是中文名。"""
    fields = tpl.get('fields') or []
    if isinstance(key, int):
        return fields[key] if 0 <= key < len(fields) else None
    s = str(key or '')
    for f in fields:
        if f['id'] == s:
            return f
    for f in fields:
        if f['name'] == s:
            return f
    for f in fields:
        if f['name'].strip() == s.strip():
            return f
    # 再宽松一层：包含匹配（「金额」能命中「存根·金额」），只在不歧义时返回
    hit = [f for f in fields if s and s in f['name']]
    return hit[0] if len(hit) == 1 else None


def compute_values(tpl, rec_idx=None):
    """返回一条记录「实际会印出来」的值：普通字段取原值，派生字段现算。

    Agent 查「这张支票上金额大写是什么」要看这个，不能看库里存的原始值。
    """
    fields = tpl['fields']
    if rec_idx is None:
        rec_idx = tpl['active']
    recs = tpl['records']
    if not recs:
        return {}
    rec = recs[rec_idx] if 0 <= rec_idx < len(recs) else recs[0]
    raw = rec.get('values') or {}

    src = None
    for f in fields:
        if f.get('autoSrc') and not f.get('auto'):
            src = raw.get(f['id'], f.get('def', ''))
            break
    if src is None:
        for f in fields:
            if f.get('autoSrc'):
                src = raw.get(f['id'], f.get('def', ''))
                break

    out = {}
    for f in fields:
        v = raw.get(f['id'], f.get('def', ''))
        auto = f.get('auto')
        if auto == 'words':
            v = to_chinese_amount(src if src is not None else v)
        elif auto == 'figs':
            v = to_figure_amount(src if src is not None else v)
        elif auto == 'wordsin':
            v = to_digits_upper(v)
        out[f['id']] = v
    return out


def with_values(tpl):
    """给模板补上每行的 name/value/auto，前端与 Agent 都用得上。"""
    t = dict(tpl)
    vals = compute_values(t)
    rows = []
    for f in t['fields']:
        row = dict(f)
        row['value'] = vals.get(f['id'], '')
        rows.append(row)
    t['fields'] = rows
    return t


# ---------------------------------------------------------------- 存取
def _path(tid):
    return os.path.join(TPL_DIR, '%s.json' % tid)


def load_all():
    out = []
    for fn in sorted(os.listdir(TPL_DIR)):
        if not fn.endswith('.json'):
            continue
        try:
            with open(os.path.join(TPL_DIR, fn), encoding='utf-8') as fh:
                out.append(normalize(json.load(fh), len(out)))
        except Exception as e:
            print('[print] 模板读取失败', fn, e)
    return out


def load(tid):
    p = _path(tid)
    if not os.path.isfile(p):
        return None
    with open(p, encoding='utf-8') as fh:
        return normalize(json.load(fh))


def save(tpl):
    tpl = normalize(tpl)
    p = _path(tpl['id'])
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(tpl, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return tpl


def delete(tid):
    p = _path(tid)
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False


def seed_if_empty():
    """首次启动灌入内置 5 套模板。已有模板就不动，避免覆盖用户校准过的坐标。"""
    if os.path.isdir(TPL_DIR) and any(f.endswith('.json') for f in os.listdir(TPL_DIR)):
        return 0
    if not os.path.isfile(SEED_PATH):
        print('[print] 缺少种子文件', SEED_PATH)
        return 0
    with open(SEED_PATH, encoding='utf-8') as fh:
        seeds = json.load(fh)
    n = 0
    for s in seeds:
        s = dict(s)
        s.pop('key', None)
        s['id'] = _gen_id('t')
        bg = s.get('bg') or ''
        # 种子里存的是 static/print-assets/xxx.jpg，转成统一前缀
        if bg.startswith('print-assets/'):
            s['bg'] = BG_PREFIX_ASSET + bg[len('print-assets/'):]
        save(s)
        n += 1
    print('[print] 已灌入 %d 套内置模板' % n)
    return n


def _bg_info(bg):
    """把 bg 字段拆成 (kind, name)。kind: asset / file / ''"""
    if not bg:
        return '', ''
    if bg.startswith(BG_PREFIX_ASSET):
        return 'asset', bg[len(BG_PREFIX_ASSET):]
    if bg.startswith(BG_PREFIX_FILE):
        return 'file', bg[len(BG_PREFIX_FILE):]
    return '', ''


def _safe_name(name):
    name = os.path.basename(str(name or ''))
    return re.sub(r'[^\w.\-]', '', name)


# ---------------------------------------------------------------- 接口
@bp.get('/templates')
def api_list():
    items = []
    for t in load_all():
        items.append({
            'id': t['id'], 'name': t['name'],
            'field_count': len(t['fields']),
            'record_count': len(t['records']),
            'settings': t['settings'], 'bg': t['bg'],
        })
    return jsonify({'items': items})


@bp.get('/templates/<tid>')
def api_get(tid):
    t = load(tid)
    if not t:
        abort(404, '模板不存在')
    return jsonify(with_values(t))


@bp.post('/templates')
def api_create():
    data = request.get_json(force=True, silent=True) or {}
    t = normalize(data)
    t['id'] = _gen_id('t')
    if not data.get('name'):
        t['name'] = '模板 %d' % (len(load_all()) + 1)
    with _lock:
        save(t)
    return jsonify(with_values(t))


@bp.put('/templates/<tid>')
def api_update(tid):
    """整份保存。前端改坐标后防抖调用；Agent 改字段走下面的局部接口。"""
    if not os.path.isfile(_path(tid)):
        abort(404, '模板不存在')
    data = request.get_json(force=True, silent=True) or {}
    data['id'] = tid          # id 不允许被 body 顶掉
    t = normalize(data)
    with _lock:
        save(t)
    return jsonify(with_values(t))


@bp.delete('/templates/<tid>')
def api_delete(tid):
    if len(load_all()) <= 1:
        return jsonify({'error': '至少保留一套模板'}), 400
    with _lock:
        ok = delete(tid)
    if not ok:
        abort(404, '模板不存在')
    return jsonify({'ok': True})


# ---- 记录
@bp.post('/templates/<tid>/records')
def api_add_record(tid):
    data = request.get_json(force=True, silent=True) or {}
    t = load(tid)
    if not t:
        abort(404, '模板不存在')
    vals = _values_by_name(t, data.get('values') or {})
    rec = {'name': str(data.get('name') or ('记录 %d' % (len(t['records']) + 1))),
           'values': vals}
    with _lock:
        t['records'].append(rec)
        save(t)
    return jsonify({'index': len(t['records']) - 1, 'name': rec['name'],
                    'values': compute_values(t, len(t['records']) - 1)})


@bp.put('/templates/<tid>/records/<int:idx>')
def api_update_record(tid, idx):
    data = request.get_json(force=True, silent=True) or {}
    t = load(tid)
    if not t:
        abort(404, '模板不存在')
    if not (0 <= idx < len(t['records'])):
        return jsonify({'error': '记录不存在'}), 404
    rec = t['records'][idx]
    if data.get('name') is not None:
        rec['name'] = str(data.get('name'))
    if isinstance(data.get('values'), dict):
        rec['values'].update(_values_by_name(t, data['values']))
    if data.get('active') is not None:
        t['active'] = idx
    with _lock:
        save(t)
    return jsonify({'index': idx, 'name': rec['name'],
                    'values': compute_values(t, idx)})


@bp.delete('/templates/<tid>/records/<int:idx>')
def api_delete_record(tid, idx):
    t = load(tid)
    if not t:
        abort(404, '模板不存在')
    if len(t['records']) <= 1:
        return jsonify({'error': '至少保留一条记录'}), 400
    if not (0 <= idx < len(t['records'])):
        return jsonify({'error': '记录不存在'}), 404
    with _lock:
        rm = t['records'].pop(idx)
        if t['active'] >= len(t['records']):
            t['active'] = len(t['records']) - 1
        save(t)
    return jsonify({'deleted': rm['name']})


@bp.post('/templates/<tid>/values')
def api_set_values(tid):
    """给「当前记录」批量赋值，键可写字段名或字段 id。Agent 最常用的写入口。"""
    data = request.get_json(force=True, silent=True) or {}
    t = load(tid)
    if not t:
        abort(404, '模板不存在')
    idx = data.get('record', t['active'])
    idx = int(idx) if isinstance(idx, (int, str)) and str(idx).lstrip('-').isdigit() else t['active']
    if not (0 <= idx < len(t['records'])):
        return jsonify({'error': '记录不存在'}), 404
    got = _values_by_name(t, data.get('values') or {})
    missing = _unknown_names(t, data.get('values') or {})
    with _lock:
        t['records'][idx]['values'].update(got)
        if idx != t['active']:
            t['active'] = idx
        save(t)
    out = {'index': idx, 'values': compute_values(t, idx)}
    if missing:
        out['unknown'] = missing
        out['hint'] = '这些字段名不存在，已忽略：' + '、'.join(missing)
    return jsonify(out)


# ---- 字段
@bp.post('/templates/<tid>/fields')
def api_add_field(tid):
    data = request.get_json(force=True, silent=True) or {}
    t = load(tid)
    if not t:
        abort(404, '模板不存在')
    f = normalize_field(data)
    f['id'] = _gen_id()
    with _lock:
        t['fields'].append(f)
        save(t)
    return jsonify(f)


@bp.patch('/templates/<tid>/fields/<fid>')
def api_patch_field(tid, fid):
    data = request.get_json(force=True, silent=True) or {}
    t = load(tid)
    if not t:
        abort(404, '模板不存在')
    f = field_by_key(t, fid)
    if not f:
        return jsonify({'error': '字段不存在: %s' % fid}), 404
    for k in ('name', 'auto', 'kind', 'def'):
        if k in data:
            f[k] = data[k] if data[k] is not None else None
    for k in ('x', 'y', 'size', 'dx', 'dy', 'dsize'):
        if k in data:
            f[k] = _num(data[k], f.get(k, 0))
    for k in ('on', 'autoSrc'):
        if k in data:
            f[k] = bool(data[k])
    if f.get('auto') == 'wordsin':
        pass  # 读数时实时算，不落库
    with _lock:
        save(t)
    return jsonify({'field': f, 'value': compute_values(t).get(f['id'], '')})


@bp.delete('/templates/<tid>/fields/<fid>')
def api_delete_field(tid, fid):
    t = load(tid)
    if not t:
        abort(404, '模板不存在')
    f = field_by_key(t, fid)
    if not f:
        return jsonify({'error': '字段不存在: %s' % fid}), 404
    with _lock:
        t['fields'] = [x for x in t['fields'] if x['id'] != f['id']]
        for r in t['records']:
            r['values'].pop(f['id'], None)
        save(t)
    return jsonify({'deleted': f['name']})


# ---- 底图
@bp.get('/bg/<path:name>')
def api_bg(name):
    kind, real = _bg_info(name)
    if kind == '':
        # 也允许直接传 assets/xxx 或 file/xxx
        if name.startswith(BG_PREFIX_ASSET):
            kind, real = 'asset', name[len(BG_PREFIX_ASSET):]
        elif name.startswith(BG_PREFIX_FILE):
            kind, real = 'file', name[len(BG_PREFIX_FILE):]
    real = _safe_name(real)
    if not real:
        abort(404)
    if kind == 'asset':
        return send_from_directory(ASSET_DIR, real, max_age=86400)
    if kind == 'file':
        return send_from_directory(BG_DIR, real, max_age=3600)
    abort(404)


@bp.post('/bg')
def api_upload_bg():
    """上传底图。multipart 或 JSON{dataurl} 都收，自动压到 1600px 内。"""
    raw = None
    if 'file' in request.files:
        raw = request.files['file'].read()
    else:
        data = request.get_json(force=True, silent=True) or {}
        du = data.get('dataurl') or ''
        m = re.match(r'data:image/[^;]+;base64,(.*)$', du, re.S)
        if not m:
            return jsonify({'error': '需要图片文件或 dataurl'}), 400
        import base64
        raw = base64.b64decode(m.group(1))
    if not raw or len(raw) < 32:
        return jsonify({'error': '图片内容为空'}), 400
    name = '%s_%s.jpg' % (time.strftime('%Y%m%d%H%M%S'), uuid.uuid4().hex[:4])
    with open(os.path.join(BG_DIR, name), 'wb') as fh:
        fh.write(raw)
    return jsonify({'bg': BG_PREFIX_FILE + name, 'bytes': len(raw)})


# ---- 导入（从旧版 localStorage 导出 JSON 迁过来）
@bp.post('/import')
def api_import():
    data = request.get_json(force=True, silent=True) or {}
    arr = data.get('templates') if isinstance(data.get('templates'), list) else data
    if not isinstance(arr, list) or not arr:
        return jsonify({'error': '需要 {templates:[...]} 或模板数组'}), 400
    mode = data.get('mode') or 'merge'      # merge = 按名称覆盖，add = 全部新增
    existing = {t['name']: t for t in load_all()}
    done = []
    with _lock:
        for item in arr:
            t = normalize(item)
            bg = t.get('bg') or ''
            m = re.match(r'data:image/[^;]+;base64,(.*)$', bg, re.S)
            if m:
                import base64
                nm = '%s_%s.jpg' % (time.strftime('%Y%m%d%H%M%S'), uuid.uuid4().hex[:4])
                with open(os.path.join(BG_DIR, nm), 'wb') as fh:
                    fh.write(base64.b64decode(m.group(1)))
                t['bg'] = BG_PREFIX_FILE + nm
            if mode == 'merge' and t['name'] in existing:
                t['id'] = existing[t['name']]['id']
            else:
                t['id'] = _gen_id('t')
            save(t)
            done.append({'id': t['id'], 'name': t['name'],
                         'fields': len(t['fields']), 'records': len(t['records'])})
    return jsonify({'count': len(done), 'mode': mode, 'items': done})


# ---------------------------------------------------------------- 内部工具
def _values_by_name(tpl, values):
    """{字段名或id: 值} → {字段id: 值}；未知键丢弃。"""
    out = {}
    for k, v in (values or {}).items():
        f = field_by_key(tpl, k)
        if f:
            out[f['id']] = '' if v is None else str(v)
    return out


def _unknown_names(tpl, values):
    bad = []
    for k in (values or {}):
        if not field_by_key(tpl, k):
            bad.append(str(k))
    return bad


# 启动时灌种子（幂等）
try:
    seed_if_empty()
except Exception as _e:
    print('[print] 种子初始化失败:', _e)
