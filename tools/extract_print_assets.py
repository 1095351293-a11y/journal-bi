# -*- coding: utf-8 -*-
"""一次性抽取：从桌面《打印模板.html》里把 5 张底图与 5 套字段定义搬进 BI。

产物：
  static/print-assets/{mop,hkd,qrd,rem,cny}.jpg   底图（随代码发布）
  print_seed/templates.json                        内置模板种子（不含底图 base64）

底图在源文件里是 dataURL(base64 jpeg)，页面直接内嵌导致 1MB。
抽成独立 jpg 后由 /api/print/bg/<name> 提供，浏览器可缓存，HTML 也瘦了。

用法：python tools/extract_print_assets.py "C:/Users/Lenovo/Desktop/打印模板/打印模板.html"
"""
import base64
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET_DIR = os.path.join(BASE, 'static', 'print-assets')
SEED_DIR = os.path.join(BASE, 'print_seed')

# 底图 key → 输出文件名。mop = 默认支票(澳门华人银行 HKD/MOP 票幅)，与页面 #bgimg 同图
ASSETS = [
    ('bgimg', 'mop'),
    ('EMBED_QRD', 'qrd'),
    ('EMBED_HKD', 'hkd'),
    ('EMBED_REM', 'rem'),
    ('EMBED_CNY', 'cny'),
]

TEMPLATES = [
    ('makeCheckTemplate', 'mop', '默认支票'),
    ('makeQrdTemplate', 'qrd', '请款单(A4横)'),
    ('makeHkTemplate', 'hkd', '香港支票(HKD)'),
    ('makeRemTemplate', 'rem', '汇款申请书(A4)'),
    ('makeCnyTemplate', 'cny', '中银汇款单(A4)'),
]

FIELDS_VAR = {
    'makeCheckTemplate': 'CHECK_FIELDS',
    'makeQrdTemplate': 'QRD_FIELDS',
    'makeHkTemplate': 'HKD_FIELDS',
    'makeRemTemplate': 'REM_FIELDS',
    'makeCnyTemplate': 'CNY_FIELDS',
}


def grab_dataurls(html):
    """返回 {'bgimg': dataurl, 'EMBED_QRD': dataurl, ...}"""
    out = {}
    m = re.search(r'<img id="bgimg" src="(data:image/[^"]+)"', html)
    if m:
        out['bgimg'] = m.group(1)
    for name in ('EMBED_QRD', 'EMBED_HKD', 'EMBED_REM', 'EMBED_CNY'):
        m = re.search(r'var %s\s*=\s*(\'data:image/[^\']+\'|"data:image/[^"]+")' % name, html)
        if m:
            out[name] = m.group(1).strip('\'"')
    return out


def js_to_json(src):
    """把 JS 对象字面量转成 JSON：给键名加引号，单引号转双引号，去尾逗号。

    字段定义是机器生成的规整写法，不用完整 JS 解析。
    """
    s = src
    s = re.sub(r'//[^\n]*', '', s)                    # 行注释
    s = re.sub(r'/\*.*?\*/', '', s, flags=re.S)       # 块注释
    s = re.sub(r",(\s*[\]\}])", r'\1', s)             # 尾逗号
    s = re.sub(r"([{,\[]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', s)  # 裸键名
    s = re.sub(r"'((?:[^'\\]|\\.)*)'", lambda m: json.dumps(m.group(1)), s)  # 单引号字符串
    return json.loads(s)


def grab_fields(html, var):
    m = re.search(r'var %s\s*=\s*(\[.*?\n  \]);' % var, html, flags=re.S)
    if not m:
        raise SystemExit('没找到字段定义: ' + var)
    return js_to_json(m.group(1))


def grab_settings(html, fn):
    """从 makeXXXTemplate 里取 settings:{...}"""
    m = re.search(r'function %s\(\).*?settings:\s*(\{[^}]*\})' % fn, html, flags=re.S)
    if not m:
        return {'w': 275, 'h': 90, 'offx': 0, 'offy': 0, 'rot': '90cw', 'showbg': True}
    return js_to_json(m.group(1))


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else r'C:/Users/Lenovo/Desktop/打印模板/打印模板.html'
    html = open(src, encoding='utf-8').read()
    os.makedirs(ASSET_DIR, exist_ok=True)
    os.makedirs(SEED_DIR, exist_ok=True)

    urls = grab_dataurls(html)
    saved = {}
    for key, name in ASSETS:
        du = urls.get(key)
        if not du:
            print('缺底图:', key)
            continue
        head, b64 = du.split(',', 1)
        ext = 'jpg' if 'jpeg' in head or 'jpg' in head else ('png' if 'png' in head else 'bin')
        raw = base64.b64decode(b64)
        path = os.path.join(ASSET_DIR, '%s.%s' % (name, ext))
        open(path, 'wb').write(raw)
        saved[name] = 'print-assets/%s.%s' % (name, ext)
        print('底图 %-5s %7d bytes  %s' % (name, len(raw), path))

    seeds = []
    for fn, asset, tname in TEMPLATES:
        fields = grab_fields(html, FIELDS_VAR[fn])
        settings = grab_settings(html, fn)
        full = []
        for i, f in enumerate(fields):
            item = {
                'id': f['id'],
                'name': f['name'],
                'x': f.get('x', 20),
                'y': f.get('y', 20),
                'size': f.get('size', 3.2),
                'on': f.get('on', True),
                'auto': f.get('auto'),
                'autoSrc': bool(f.get('autoSrc')),
                'kind': f.get('kind'),
                'dx': f.get('x', 20),
                'dy': f.get('y', 20),
                'dsize': f.get('size', 3.2),
                'def': f.get('def', ''),
            }
            full.append(item)
        seeds.append({
            'key': fn.replace('make', '').replace('Template', '').lower(),
            'name': tname,
            'bg': saved.get(asset, ''),
            'fields': full,
            'records': [{'name': '记录 1', 'values': {}}],
            'active': 0,
            'settings': settings,
        })
        print('模板 %-14s %2d 字段  %s' % (tname, len(full), settings))

    out = os.path.join(SEED_DIR, 'templates.json')
    json.dump(seeds, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('种子 ->', out)


if __name__ == '__main__':
    main()
