# -*- coding: utf-8 -*-
"""附件处理：图片 / CSV / Excel / PDF / DOCX / 文本

只做三件事：落盘、转成模型能吃的东西、把原始文件还能给前端预览。
解析库都做成可选依赖，缺了不崩，只是降级成「读不了这个格式」。
"""
import base64
import io
import json
import mimetypes
import os
import re
import uuid
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
UP_DIR = os.path.join(DATA_DIR, 'agent_uploads')

MAX_BYTES = 25 * 1024 * 1024
TEXT_LIMIT = 20000
IMAGE_EDGE = 1400

IMAGE_EXT = {'png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'}
TEXT_EXT = {'txt', 'md', 'markdown', 'json', 'log', 'csv', 'tsv', 'xml', 'html'}
OK_EXT = IMAGE_EXT | TEXT_EXT | {'xlsx', 'xls', 'pdf', 'docx'}


def _ensure():
    os.makedirs(UP_DIR, exist_ok=True)


def _ext(name):
    return os.path.splitext(str(name or ''))[1].lower().lstrip('.')


def _read_text(path, limit=TEXT_LIMIT):
    raw = open(path, 'rb').read()
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'big5', 'latin-1'):
        try:
            return raw.decode(enc)[:limit]
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')[:limit]


def _csv_preview(path, ext, limit=TEXT_LIMIT):
    import csv as _csv
    txt = _read_text(path, limit * 3)
    sep = '\t' if ext == 'tsv' else ','
    raw = [l for l in txt.replace('\r', '').split('\n') if l.strip()]
    if not raw:
        return '（空文件）'
    rows = list(_csv.reader(raw, delimiter=sep))
    if not rows:
        return '（空文件）'
    # 对账单常见「前几行是标题/账户信息」，列数最多的那行才是表头
    hi, best = 0, 0
    for i, r in enumerate(rows[:8]):
        if len(r) > best:
            hi, best = i, len(r)
    cols = [c.strip()[:30] for c in rows[hi][:24]]
    out = ['| ' + ' | '.join(cols) + ' |', '|' + '---|' * len(cols)]
    body = rows[hi + 1:]
    for r in body[:80]:
        cells = [(r[k] if k < len(r) else '').strip()[:40] for k in range(len(cols))]
        out.append('| ' + ' | '.join(cells) + ' |')
    more = len(body) - 80
    if more > 0:
        out.append(f'\n（还有 {more} 行未列出，共 {len(body)} 行数据）')
    return '\n'.join(out)[:limit]


def _xlsx_preview(path, limit=TEXT_LIMIT):
    try:
        import openpyxl
    except ImportError:
        return '（本机缺少 openpyxl，读不了 xlsx）'
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    chunks = []
    for ws in wb.worksheets[:4]:
        rows, n = [], 0
        for row in ws.iter_rows(values_only=True):
            vals = ['' if v is None else str(v)[:40] for v in row[:20]]
            if not any(str(v).strip() for v in vals):
                continue
            rows.append('| ' + ' | '.join(vals) + ' |')
            n += 1
            if n >= 40:
                break
        if rows:
            chunks.append(f'### 工作表：{ws.title}（约 {ws.max_row} 行 × {ws.max_column} 列）\n'
                          + '\n'.join(rows))
    wb.close()
    return ('\n\n'.join(chunks) or '（工作簿是空的）')[:limit]


def _xls_preview(path, limit=TEXT_LIMIT):
    try:
        import xlrd
    except ImportError:
        return '（本机缺少 xlrd，读不了 xls；可另存为 xlsx 再传）'
    book = xlrd.open_workbook(path)
    chunks = []
    for sh in book.sheets()[:4]:
        rows = []
        for r in range(min(sh.nrows, 40)):
            vals = [str(sh.cell_value(r, c))[:40] for c in range(min(sh.ncols, 20))]
            if any(v.strip() for v in vals):
                rows.append('| ' + ' | '.join(vals) + ' |')
        if rows:
            chunks.append(f'### 工作表：{sh.name}（{sh.nrows} 行 × {sh.ncols} 列）\n'
                          + '\n'.join(rows))
    return ('\n\n'.join(chunks) or '（工作簿是空的）')[:limit]


def _pdf_preview(path, limit=TEXT_LIMIT):
    try:
        import fitz
    except ImportError:
        try:
            import pymupdf as fitz
        except ImportError:
            return '（本机缺少 PDF 解析库，读不了 PDF）'
    doc = fitz.open(path)
    out = []
    for i, page in enumerate(doc):
        if i >= 10:
            out.append(f'\n（共 {doc.page_count} 页，只读了前 10 页）')
            break
        t = (page.get_text() or '').strip()
        if t:
            out.append(f'### 第 {i + 1} 页\n{t}')
    doc.close()
    return ('\n\n'.join(out) or '（PDF 里没有可提取的文字，可能是扫描件；'
            '可以把页面截图当图片上传）')[:limit]


def _docx_preview(path, limit=TEXT_LIMIT):
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read('word/document.xml').decode('utf-8', 'replace')
    except (KeyError, zipfile.BadZipFile, OSError):
        return '（读不了这个 docx）'
    xml = re.sub(r'</w:p>', '\n', xml)
    xml = re.sub(r'<w:tab[^>]*/>', '\t', xml)
    text = re.sub(r'<[^>]+>', '', xml)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()[:limit]
_IMG_CACHE = {}


def _image_meta(path, ext):
    try:
        from PIL import Image
        im = Image.open(path)
        w, h = im.size
        if max(w, h) > IMAGE_EDGE:
            im.thumbnail((IMAGE_EDGE, IMAGE_EDGE))
        if im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=82, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        return {'width': w, 'height': h,
                'data_url': 'data:image/jpeg;base64,' + b64,
                'bytes_sent': len(b64)}
    except Exception:
        return {}


def parse_file(path, ext):
    if ext in IMAGE_EXT:
        return ''
    if ext in ('csv', 'tsv'):
        return _csv_preview(path, ext)
    if ext in TEXT_EXT:
        return _read_text(path)
    if ext == 'xlsx':
        return _xlsx_preview(path)
    if ext == 'xls':
        return _xls_preview(path)
    if ext == 'pdf':
        return _pdf_preview(path)
    if ext == 'docx':
        return _docx_preview(path)
    return ''


def save_upload(fs):
    _ensure()
    name = os.path.basename(str(fs.filename or 'file'))
    ext = _ext(name)
    if ext not in OK_EXT:
        raise ValueError(f'不支持的文件类型 .{ext}，'
                         f'目前支持图片、CSV/TSV、Excel、PDF、DOCX、TXT/MD/JSON')
    raw = fs.read()
    if len(raw) > MAX_BYTES:
        raise ValueError(f'文件太大（{len(raw) / 1048576:.1f}MB），上限 25MB')
    if not raw:
        raise ValueError('文件是空的')
    fid = uuid.uuid4().hex
    path = os.path.join(UP_DIR, f'{fid}.{ext}')
    with open(path, 'wb') as f:
        f.write(raw)
    kind = 'image' if ext in IMAGE_EXT else (
        'sheet' if ext in ('xlsx', 'xls', 'csv', 'tsv') else 'doc')
    extract = ''
    try:
        extract = parse_file(path, ext)
    except Exception as exc:
        extract = f'（解析失败：{type(exc).__name__}: {exc}）'
    meta = {'fid': fid, 'name': name, 'ext': ext, 'kind': kind,
            'size': len(raw), 'extract': extract,
            'extract_len': len(extract), 'ts': time_now()}
    if kind == 'image':
        meta.update({k: v for k, v in _image_meta(path, ext).items()
                     if k != 'data_url'})
    with open(os.path.join(UP_DIR, f'{fid}.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False)
    pub = dict(meta)
    pub.pop('extract', None)
    pub['url'] = f'/api/agent/file/{fid}'
    pub['preview'] = (extract[:400] if kind != 'image' else '')
    return pub


def time_now():
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _meta_path(fid):
    if not re.fullmatch(r'[a-f0-9]{32}', str(fid or '')):
        return None
    return os.path.join(UP_DIR, f'{fid}.json')


def load_att(fid):
    mp = _meta_path(fid)
    if not mp or not os.path.isfile(mp):
        return None
    with open(mp, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    meta['path'] = os.path.join(UP_DIR, f"{meta['fid']}.{meta['ext']}")
    if meta.get('kind') == 'image':
        if fid not in _IMG_CACHE:
            _IMG_CACHE[fid] = _image_meta(meta['path'], meta['ext'])
        meta.update(_IMG_CACHE[fid])
    return meta


def serve_file(fid):
    from flask import send_file
    mp = _meta_path(fid)
    if not mp or not os.path.isfile(mp):
        raise FileNotFoundError('文件不存在或已过期')
    with open(mp, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    path = os.path.join(UP_DIR, f"{meta['fid']}.{meta['ext']}")
    if not os.path.isfile(path):
        raise FileNotFoundError('文件已被删除')
    mt = mimetypes.guess_type(meta['name'])[0] or 'application/octet-stream'
    return send_file(path, mimetype=mt, download_name=meta['name'],
                     as_attachment=False)
