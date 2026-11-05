# -*- coding: utf-8 -*-
"""Agent 内核 —— 会思考、会调工具、边干边说的查账助手

与 agent.py 的分工
------------------
* agent.py：数据工具（只读 SQLite）+ 旧的非流式端点，保持兼容。
* 本模块：真正的 Agent 循环。意图路由（先判断要不要查库）、会话事实缓存
  （查过一次就记住）、流式输出（思考过程 + 工具调用 + 正文逐字），
  以及文件/图片附件的接入。

对外只暴露 SSE 端点 /api/agent/chat/stream，前端按事件渲染。
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime

from flask import Blueprint, Response, jsonify, request, stream_with_context

from agent import (TOOLS, TOOL_IMPL, TOOL_LABEL, WRITE_NAMES, db, load_config,
                   mask_key, money, one, rows, PRESETS)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')

# 会话事实缓存：sid -> {'facts': {key: {...}}, 'ts': 时间戳}
SESS = {}
SESS_TTL = 30 * 60          # 30 分钟内查过的数据可以直接复用
SESS_MAX = 200
FACTS_IN_PROMPT = 8          # 最多注入 8 条已知事实
LINK_TOTAL = 6


# ---------------------------------------------------------------- 意图路由
_RE_GREET = re.compile(
    r'^(你好|您好|hi|hello|嗨|哈喽|在吗|在不在|谢谢|多谢|感谢|好的|好|ok|嗯+|收到|'
    r'明白|拜拜|再见|早|晚上好|下午好)[\s!！。,.，~？?]*$', re.I)
_RE_CAP = re.compile(
    r'(你|您).{0,4}(能|会|可以|支持).{0,10}(做什么|干什么|什么用|哪些|什么功能|怎么用|'
    r'能否|吗)|^(帮助|help|使用说明|怎么用)[\s!！。?]*$', re.I)
_RE_REFER = re.compile(
    r'(刚才|刚刚|上面|上一条|上一轮|前面|之前|这个结果|这条|这笔|这些|那份|它|'
    r'其中|继续|接着|展开|详细说|整理成|做成|转成|换成|改成|翻译|总结一下|'
    r'汇总一下|导出|复制|美化|排一下序|加一列)')
_RE_DATA = re.compile(
    r'(余额|多少钱|结余|支出|收入|流水|明细|笔数|几笔|账户|公司|定期|存款|到期|'
    r'利息|盘点|库存现金|标签|收据|趋势|对比|排名|最多|最少|占比|比例|对账|'
    r'银行|支票|转账|汇率|折算|本月|这个月|上个?月|下个?月|今年|去年|季度|'
    r'上旬|中旬|下旬|\d{4}\s*[-/年]\s*\d{1,2}|\d{1,2}\s*月|\d{4}\s*年)')
_RE_AGG = re.compile(r'(按|分别|分组|各个|每个|排名|排序|占比|趋势|同比|环比|top\d*)')
# 写入意图：出现这些说法才挂写工具，避免纯查询时模型乱起草
_RE_WRITE = re.compile(
    r'(记一笔|记一下|帮我记|帮我加|补记|记入|入账|录入|新增一笔|加一笔|添加一笔|'
    r'录一笔|做一笔|开一笔|这笔补|登记一下|'
    r'改成|改为|修改|调整成|更新一下这|把.{0,12}(摘要|备注|对方|项目|金额|日期|标签)'
    r'.{0,6}(改|换|调)|打标签|加标签|标记为|标一下|'
    r'删掉|删除|去掉这|移除这|作废这|重复了|'
    r'导入|批量加|批量录|把.{0,10}(对账单|账单|表).{0,6}(导入|录|记))')
_RE_NO_WRITE = re.compile(r'(导出|下载|打印|怎么记|如何记|能不能记|可以记)')
# 出现这些词，说明问题本身定义了新的数据范围，不能只靠已有事实回答
_RE_SCOPE = re.compile(
    r'(\d{4}\s*[-/年]\s*\d{1,2}|\d{1,2}\s*月|本月|这个月|上个?月|今年|去年|'
    r'全部|所有|各家|所有公司|\d[\d,]{2,})')

_TOOLSET_RULES = [
    (r'(定期|存款|到期|利息|理财)', ['list_deposits']),
    (r'(盘点|库存现金|实盘|现钞)', ['list_cash_counts', 'list_accounts']),
    (r'(标签|收据|待办|未签字|跟进)', ['list_tags']),
    (r'(余额|多少钱|结余|可用|资金|账上)', ['list_accounts', 'account_balance',
                                            'company_summary']),
    (r'(趋势|对比|排名|占比|最多|最少|按月|每个月|同比|环比|分类|分组|top)',
     ['aggregate_entries', 'compare_periods', 'query_entries']),
    (r'(流水|明细|支出|收入|付款|收款|交易|支票|转账|笔)', ['query_entries']),
]


def route_intent(text, has_facts=False, has_atts=False):
    """决定这次要不要查库、给模型看哪些工具。

    返回 {'mode': 'direct'|'context'|'agent', 'reply': 直答文案, 'tools': 工具名列表或 None}
    """
    t = (text or '').strip()
    if not t:
        return {'mode': 'direct', 'reply': '说说你想了解什么？', 'tools': []}
    if has_atts:
        # 带了附件就是要干活：读图 + 核账（可能还要顺手记一笔），工具全开
        return {'mode': 'agent', 'tools': None, 'write': True}
    if _RE_WRITE.search(t) and not _RE_NO_WRITE.search(t):
        # 写入意图：先让它查清账户/流水，再起草，工具全开
        return {'mode': 'agent', 'tools': None, 'write': True}
    if _RE_GREET.match(t):
        return {'mode': 'direct', 'tools': [],
                'reply': '在。账上的事直接问我——余额、某笔流水、月度收支、大额支出、'
                         '未兑现支票、定期到期，或者把银行回单截图/对账单丢给我看。'}
    if _RE_CAP.search(t):
        return {'mode': 'direct', 'tools': [],
                'reply': '我能自己动手查这套账，不用你翻页面：\n'
                         '- **查数据**：余额、流水明细、月度收支、趋势排名、大额支出、'
                         '未兑现/未到账、定期到期、现金盘点、标签待办\n'
                         '- **算账**：两期对比（环比/同比）、按对方/项目/账户分组占比\n'
                         '- **看文件**：上传银行回单截图、Excel/CSV 对账单、PDF 月结单，'
                         '我能读出来核对\n'
                         '- **干活**：查到的记录直接给我链接，点一下跳到对应流水\n\n'
                         '直接说人话就行，比如「上个月 MOP 支票户支出最多的五笔」。'}
    need_data = bool(_RE_DATA.search(t) or _RE_AGG.search(t))
    scope = bool(_RE_SCOPE.search(t))
    refer = bool(_RE_REFER.search(t))

    if refer and not scope and not need_data:
        # 「把刚才那个做成表格」——纯加工，手上的结果够用，别再打库
        return {'mode': 'context', 'tools': []}
    if not need_data and not scope and len(t) <= 30 and not refer:
        # 短问题且没有任何数据信号：交给模型自己判断，但先不给工具
        return {'mode': 'context', 'tools': []}

    names = []
    for pat, tools in _TOOLSET_RULES:
        if re.search(pat, t, re.I):
            for x in tools:
                if x not in names:
                    names.append(x)
    if not names:
        return {'mode': 'agent', 'tools': None}      # 全部工具，模型自选
    for base in ('data_overview',):
        if base not in names:
            names.insert(0, base)
    return {'mode': 'agent', 'tools': names}


def pick_tools(names, allow_write=False):
    """按意图裁剪工具。写入工具只在明确要改账时才挂上，纯查询时不给。"""
    pool = TOOLS if allow_write else [
        t for t in TOOLS if t['function']['name'] not in WRITE_NAMES]
    if names is None:
        return pool
    keep = set(names) | {'show_links'}
    return [t for t in pool if t['function']['name'] in keep]


# ---------------------------------------------------------------- 会话事实
_lock = threading.Lock()


def sess_get(sid):
    now = time.time()
    with _lock:
        d = SESS.get(sid)
        if d and now - d.get('ts', 0) < SESS_TTL:
            d['ts'] = now
            return d
        if len(SESS) > SESS_MAX:          # 简单清理，别无限长
            for k in sorted(SESS, key=lambda k: SESS[k].get('ts', 0))[:50]:
                SESS.pop(k, None)
        d = {'facts': {}, 'ts': now}
        SESS[sid] = d
        return d


def sess_add_fact(sid, key, label, digest, payload=None):
    d = sess_get(sid)
    d['facts'][key] = {'label': label, 'digest': digest,
                       'payload': (payload or '')[:6000],
                       'ts': datetime.now().strftime('%H:%M')}
    if len(d['facts']) > 30:
        for k in list(d['facts'])[:-30]:
            d['facts'].pop(k, None)
# ---------------------------------------------------------------- 模型能力
_VISION_HINT = re.compile(
    r'(vl\b|vl-|vision|4o\b|4\.1|omni|claude|gemini|internvl|llava|minicpm-v|'
    r'glm-4v|qwen.*vl|step-1v|moonshot-v1.*vision|flash)', re.I)


def vision_capable(model_or_cfg):
    """是否能把图片直接喂给模型。

    优先级：配置里显式写的 vision（设置页可勾选/实测）> 模型名猜测。
    模型名换代太快，猜错了用户可以在设置里手动勾上，不用改代码。
    """
    if isinstance(model_or_cfg, dict):
        cfg = model_or_cfg
        if cfg.get('vision') is not None:
            return bool(cfg['vision'])
        model = cfg.get('model')
    else:
        model = model_or_cfg
    return bool(_VISION_HINT.search(str(model or '')))


def _endpoint(base_url):
    u = (base_url or '').strip().rstrip('/')
    if not u:
        raise ValueError('未配置 API 地址（Base URL）')
    if u.endswith('/chat/completions'):
        return u
    return u + '/chat/completions'


def _headers(cfg):
    h = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
    key = (cfg.get('api_key') or '').strip()
    if key:
        h['Authorization'] = 'Bearer ' + key
    return h


def _http_error(e, cfg):
    detail = ''
    try:
        detail = e.read().decode('utf-8', 'replace')[:600]
    except Exception:
        pass
    msg = f'接口返回 HTTP {e.code}'
    try:
        j = json.loads(detail)
        em = (j.get('error') or {}).get('message') or j.get('message')
        if em:
            msg += f'：{em}'
    except ValueError:
        if detail:
            msg += f'：{detail}'
    if e.code in (401, 403):
        msg += '（API Key 可能无效或无权限）'
    elif e.code == 404:
        msg += '（Base URL 或模型名可能不对，有的服务要带 /v1）'
    elif e.code == 429:
        msg += '（限流或余额不足）'
    return RuntimeError(msg)


def llm_stream(cfg, messages, tools=None):
    """OpenAI 兼容的流式调用。yield (kind, data)：
    think / text / tool / finish / usage
    """
    payload = {'model': cfg['model'], 'messages': messages,
               'temperature': float(cfg.get('temperature') or 0), 'stream': True}
    if tools:
        payload['tools'] = tools
        payload['tool_choice'] = 'auto'
    req = urllib.request.Request(
        _endpoint(cfg.get('base_url')),
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers=_headers(cfg), method='POST')
    timeout = max(10, int(cfg.get('timeout') or 90))
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise _http_error(e, cfg)
    except urllib.error.URLError as e:
        raise RuntimeError(f'连不上模型服务：{e.reason}。检查 Base URL、网络/代理；'
                           f'本地 Ollama 需先启动。')
    except TimeoutError:
        raise RuntimeError(f'模型响应超时（>{timeout}s）。')
    with resp:
        for raw in resp:
            line = raw.decode('utf-8', 'replace').strip()
            if not line or line.startswith(':') or not line.startswith('data:'):
                continue
            body = line[5:].strip()
            if body in ('[DONE]', 'DONE'):
                break
            try:
                j = json.loads(body)
            except ValueError:
                continue
            if isinstance(j, dict) and j.get('error'):
                err = j['error']
                raise RuntimeError(str(err.get('message') if isinstance(err, dict)
                                       else err))
            if j.get('usage'):
                yield ('usage', j['usage'])
            for ch in j.get('choices') or []:
                d = ch.get('delta') or {}
                think = d.get('reasoning_content') or d.get('reasoning') or ''
                if think:
                    yield ('think', think)
                if d.get('content'):
                    yield ('text', d['content'])
                for tc in d.get('tool_calls') or []:
                    yield ('tool', tc)
                if ch.get('finish_reason'):
                    yield ('finish', ch['finish_reason'])
# ---------------------------------------------------------------- 提示词组装
def load_system_template():
    p = os.path.join(BASE_DIR, 'prompts', 'agent_system.md')
    try:
        with open(p, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ('你是日记账 BI 系统的 AI 助手。所有金额、笔数必须来自工具，'
                '不得编造。今天是 {today}，默认公司 {company}。{extra}')


def facts_block(sid):
    d = sess_get(sid)
    facts = list(d['facts'].items())[-FACTS_IN_PROMPT:]
    if not facts:
        return ''
    lines = ['', '## 本次会话已查到的数据（可以直接引用，别重复查）']
    for _k, f in facts:
        lines.append(f"- [{f['ts']}] {f['digest']}")
    lines.append('上面这些是本次对话已经查过的结果。问题能答就直接答，'
                 '并注明「依据 xx:xx 的查询」；确实缺数据时再调工具，'
                 '同条件不要重复查。')
    return '\n'.join(lines)


def build_system(sid, company, att_block=''):
    try:
        tmpl = load_system_template()
    except Exception:
        tmpl = ''
    extra = (att_block or '') + facts_block(sid)
    out = tmpl.replace('{today}', date.today().isoformat())
    out = out.replace('{company}', company or '（未设置）')
    out = out.replace('{extra}', extra)
    return out


def build_user_content(text, atts, cfg):
    """无附件返回 str；有附件返回多模态 parts。"""
    atts = atts or []
    if not atts:
        return text
    vision = vision_capable(cfg)
    parts = [{'type': 'text', 'text': text or '请看我上传的文件'}]
    for a in atts:
        kind = a.get('kind')
        name = a.get('name') or '附件'
        if kind == 'image':
            if vision and a.get('data_url'):
                parts.append({'type': 'image_url',
                              'image_url': {'url': a['data_url']}})
                parts.append({'type': 'text',
                              'text': f"【图片：{name}】请先看清图里的关键字段再回答。"})
            else:
                parts.append({'type': 'text', 'text':
                              f"【图片：{name}】当前模型（{cfg.get('model')}）被判定为不支持读图，"
                              f"你只能看到文件名。请提醒用户：设置页勾选「模型支持读图」后重试，"
                              f"或换成 qwen-vl-max / gpt-4o / glm-4v 这类视觉模型。"})
        else:
            body = a.get('extract') or ''
            tip = ''
            if kind == 'sheet':
                tip = (f"\n（表格文件，fid={a.get('fid')}。要导进账本就直接调 "
                       f"import_file(fid=\"{a.get('fid')}\", account=...) ，"
                       f"不要凭预览逐条编，预览可能被截断）")
            parts.append({'type': 'text',
                          'text': f"【附件：{name}】fid={a.get('fid')}{tip}\n{body[:14000]}"})
    return parts
# ---------------------------------------------------------------- 工具执行与摘要
def _fmt(v):
    try:
        return f'{float(v or 0):,.2f}'
    except (TypeError, ValueError):
        return str(v)


def digest_of(name, args, result):
    """把工具结果压成一句人类可读的摘要，存进会话事实供后续轮次复用。"""
    if not isinstance(result, dict) or result.get('error'):
        return ''
    try:
        if name == 'query_entries':
            head = (f"流水查询：共 {result.get('total')} 笔，收 {_fmt(result.get('sum_in'))}"
                    f" / 支 {_fmt(result.get('sum_out'))}，区间 "
                    f"{result.get('date_min')}~{result.get('date_max')}")
            items = result.get('entries') or []
            if items:
                sample = '；'.join(
                    f"{e.get('date')} {(e.get('description') or '')[:18]}"
                    f" {(e.get('debit_in') or e.get('credit_out')):,.2f}"
                    for e in items[:5])
                head += f"。前 {len(items[:5])} 笔：{sample}"
            return head
        if name == 'aggregate_entries':
            rs = result.get('rows') or []
            top = '；'.join(f"{r['key']} 支 {_fmt(r['out'])}（{r['share_out']}%）"
                            for r in rs[:6])
            return (f"聚合 {result.get('group_by')}：共 {result.get('total', {}).get('count')} 笔，"
                    f"收 {_fmt(result.get('total', {}).get('in'))} / "
                    f"支 {_fmt(result.get('total', {}).get('out'))}。TOP：{top}")
        if name == 'compare_periods':
            seg = '；'.join(
                f"{c['currency']} 收入变动 {_fmt(c['delta_in'])}、支出变动 "
                f"{_fmt(c['delta_out'])}、净额变动 {_fmt(c['delta_net'])}"
                for c in (result.get('compare') or [])[:4])
            return f"两期对比（{result.get('period_a')} 对比 {result.get('period_b')}）：{seg}"
        if name == 'account_balance':
            return (f"{result.get('company')} · {result.get('account')}"
                    f"（{result.get('currency')}）截至 {result.get('as_of')}："
                    f"账面 {_fmt(result.get('balance'))}，"
                    f"实际可用 {_fmt(result.get('actual_balance'))}")
        if name == 'data_overview':
            e = result.get('entries') or {}
            names = '、'.join(c['name'] for c in (result.get('companies') or [])[:6])
            return (f"数据总览：{result.get('company_count')} 家公司（{names}），"
                    f"{result.get('account_count')} 个账户，{e.get('count')} 笔流水，"
                    f"区间 {e.get('date_min')}~{e.get('date_max')}，"
                    f"定期 {result.get('deposit_count')} 笔")
        if name == 'list_accounts':
            accs = result.get('accounts') or []
            seg = '；'.join(f"{a['name']}({a['currency']}) {_fmt(a.get('actual_balance'))}"
                            for a in accs[:8])
            return f"账户 {len(accs)} 个，实际余额：{seg}"
        if name == 'company_summary':
            return f"公司汇总 {len(result.get('summaries') or [])} 家"
        if name == 'list_deposits':
            ds = result.get('deposits') or []
            seg = '；'.join(f"{d['company']} {_fmt(d['principal'])} {d['currency']} "
                            f"{d['end_date']} 剩 {d['days_to_maturity']} 天"
                            for d in ds[:5])
            return f"定期 {len(ds)} 笔：{seg}"
        if name == 'list_tags':
            ts = result.get('tags') or []
            return '标签：' + '、'.join(f"{t['name']}({t['n']})" for t in ts[:10])
        if name == 'list_cash_counts':
            return f"现金盘点 {result.get('count')} 张"
        if name in WRITE_NAMES:
            if result.get('error'):
                return ''
            n = result.get('count') or 0
            w = len(result.get('warnings') or [])
            return (f"已生成草稿：{result.get('draft', '')[:120]}"
                    + (f"（{w} 条提醒）" if w else '') + ' · 等用户确认')
    except Exception:
        return ''
    return ''


def run_tool(name, args, sid=''):
    impl = TOOL_IMPL.get(name)
    if not impl:
        return {'error': f'未知工具 {name}'}, []
    t0 = time.time()
    a = dict(args) if isinstance(args, dict) else {}
    if sid:
        a['__sid'] = sid          # 写入工具靠它把草稿挂到当前会话
    try:
        result, links = impl(a)
    except Exception as exc:
        result, links = {'error': f'{type(exc).__name__}: {exc}'}, []
    return result, links, int((time.time() - t0) * 1000)
# ---------------------------------------------------------------- Agent 主循环
def _company_name():
    try:
        r = one('SELECT name FROM companies ORDER BY sort, id LIMIT 1')
        return r['name'] if r else ''
    except Exception:
        return ''


def _typewriter(s):
    step = 14
    for i in range(0, len(s), step):
        yield s[i:i + step]
        time.sleep(0.008)


def run_agent_stream(cfg, sid, history, text, atts=None):
    """yield 事件 dict；由 SSE 层序列化。"""
    t0 = time.time()
    atts = atts or []
    try:
        d = sess_get(sid)
        route = route_intent(text, bool(d['facts']), bool(atts))
        yield {'t': 'route', 'mode': route['mode'],
               'label': ('看图并核对账目' if atts else
                         {'direct': '直接回答', 'context': '基于已有结果',
                          'agent': '需要查数据'}.get(route['mode'], '')),
               'tools': [TOOL_LABEL.get(x, x) for x in (route.get('tools') or [])
                         if x != 'show_links']}

        if route['mode'] == 'direct':
            for chunk in _typewriter(route['reply']):
                yield {'t': 'text', 'd': chunk}
            yield {'t': 'done', 'model': cfg.get('model'),
                   'ms': int((time.time() - t0) * 1000), 'turns': 0,
                   'usage': {'total_tokens': 0}}
            return

        # 历史 + 本轮输入
        messages = [{'role': 'system',
                     'content': build_system(sid, _company_name())}]
        for h in (history or [])[-12:]:
            role = h.get('role')
            if role in ('user', 'assistant') and h.get('content'):
                messages.append({'role': role,
                                 'content': str(h['content'])[:4000]})
        messages.append({'role': 'user',
                         'content': build_user_content(text, atts, cfg)})

        tools = pick_tools(route.get('tools'), route.get('write', False))
        usage = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
        auto_links, pushed_links = [], []
        rounds = max(1, min(int(cfg.get('max_rounds') or 6), 12))
        reply = ''
        turns = 0
        last_text = ''

        for turn in range(rounds):
            turns = turn + 1
            turn_cache = {}
            yield {'t': 'phase', 'name': '思考中' if turn == 0 else '继续分析'}
            calls = {}
            turn_text = ''
            for kind, val in llm_stream(cfg, messages, tools):
                if kind == 'think':
                    yield {'t': 'think', 'd': val}
                elif kind == 'text':
                    turn_text += val
                    yield {'t': 'text', 'd': val}
                elif kind == 'tool':
                    i = val.get('index', 0)
                    slot = calls.setdefault(i, {'id': '', 'name': '', 'args': ''})
                    if val.get('id'):
                        slot['id'] = val['id']
                    fn = val.get('function') or {}
                    if fn.get('name'):
                        slot['name'] += fn['name']
                    if fn.get('arguments'):
                        slot['args'] += fn['arguments']
                elif kind == 'usage':
                    for k in usage:
                        try:
                            usage[k] += int(val.get(k) or 0)
                        except (TypeError, ValueError, AttributeError):
                            pass
            if not calls:
                last_text = turn_text
                reply = turn_text
                yield {'t': 'phase', 'name': ''}
                break
            # 模型在调工具前说的过渡话，归入思考区，不当作正式回答
            if turn_text.strip():
                yield {'t': 'flush_think'}
            ordered = [calls[k] for k in sorted(calls)]
            messages.append({
                'role': 'assistant', 'content': turn_text or '',
                'tool_calls': [{'id': c['id'] or f'call_{i}', 'type': 'function',
                                'function': {'name': c['name'], 'arguments': c['args'] or '{}'}}
                               for i, c in enumerate(ordered)]})
            for i, c in enumerate(ordered):
                name = c['name']
                try:
                    cargs = json.loads(c['args'] or '{}')
                except ValueError:
                    cargs = {}
                if not isinstance(cargs, dict):
                    cargs = {}
                cid = c['id'] or f'call_{turn}_{i}'
                yield {'t': 'tool', 'state': 'start', 'id': cid, 'name': name,
                       'label': TOOL_LABEL.get(name, name)}
                ck = name + '|' + json.dumps(cargs, sort_keys=True,
                                             ensure_ascii=False)
                if ck in turn_cache:      # 同一轮里模型重复发同一个调用，直接复用
                    result, links, ms, ui = turn_cache[ck]
                else:
                    result, links, ms = run_tool(name, cargs, sid)
                    # 草稿明细只推前端，不进模型上下文（批量 200 行会把窗口撑爆）
                    ui = None
                    if isinstance(result, dict):
                        ui = result.pop('__ui', None)
                    turn_cache[ck] = (result, links, ms, ui)
                ok = not (isinstance(result, dict) and result.get('error'))
                summary = digest_of(name, cargs, result) or (
                    '出错了：' + str(result.get('error'))[:120] if not ok else '完成')
                yield {'t': 'tool', 'state': 'end', 'id': cid, 'name': name,
                       'label': TOOL_LABEL.get(name, name), 'ms': ms,
                       'ok': ok, 'summary': summary[:300]}
                if ui and ui.get('proposal'):
                    yield {'t': 'proposal', **ui}
                bucket = pushed_links if name == 'show_links' else auto_links
                for l in links:
                    if not isinstance(l, dict):
                        continue
                    tgt = str(l.get('target') or '').strip()
                    if not tgt or any(x['target'] == tgt for x in bucket):
                        continue
                    bucket.append({'label': str(l.get('label') or tgt)[:120],
                                   'target': tgt})
                if ok and name not in ('show_links',):
                    key = name + '|' + json.dumps(cargs, sort_keys=True,
                                                  ensure_ascii=False)[:200]
                    sess_add_fact(sid, key, TOOL_LABEL.get(name, name), summary,
                                  json.dumps(result, ensure_ascii=False)[:4000])
                messages.append({'role': 'tool', 'tool_call_id': cid,
                                 'content': json.dumps(result, ensure_ascii=False)[:16000]})
        else:
            if not reply:
                reply = last_text or ('已经查了几轮还没收敛，把问题拆小一点会更快，'
                                      '比如先问「有哪些账户」，再问具体那个账户的流水。')
                yield {'t': 'text', 'd': reply}

        if not reply:
            reply = '（模型没有返回内容，可以点重新生成或换个模型试试）'
            yield {'t': 'text', 'd': reply}
        inline = bool(re.search(r'\]\(#[A-Za-z]+:', reply))
        shown = [] if inline else (pushed_links or auto_links)
        if shown:
            yield {'t': 'links', 'links': shown[:LINK_TOTAL]}
        yield {'t': 'done', 'model': cfg.get('model'),
               'ms': int((time.time() - t0) * 1000), 'turns': turns,
               'usage': usage}
    except Exception as exc:
        yield {'t': 'error', 'msg': str(exc)}
# ---------------------------------------------------------------- HTTP 端点
bp = Blueprint('agent_core', __name__)


def sse(obj):
    return 'data: ' + json.dumps(obj, ensure_ascii=False) + '\n\n'


def _cfg_ready(cfg):
    if not cfg.get('base_url') or not cfg.get('model'):
        return '还没配置模型，请到「设置 → AI 模型」填写 Base URL 和模型名'
    url = cfg.get('base_url') or ''
    if not (cfg.get('api_key') or '').strip() and 'localhost' not in url \
            and '127.0.0.1' not in url:
        return '还没填 API Key，请到「设置 → AI 模型」填写'
    return ''


@bp.post('/api/agent/chat/stream')
def chat_stream():
    data = request.get_json(force=True) or {}
    text = str(data.get('message') or '').strip()
    if not text:
        return jsonify({'error': '请输入问题'}), 400
    cfg = load_config()
    err = _cfg_ready(cfg)
    if err:
        return jsonify({'error': err}), 400
    sid = str(data.get('sid') or 'default')[:64]
    atts = []
    try:
        from agent_files import load_att
        for fid in (data.get('attachments') or [])[:6]:
            a = load_att(str(fid))
            if a:
                atts.append(a)
    except Exception:
        atts = []

    def gen():
        try:
            yield sse({'t': 'start', 'model': cfg.get('model'),
                       'vision': vision_capable(cfg)})
            for ev in run_agent_stream(cfg, sid, data.get('history') or [],
                                       text, atts):
                yield sse(ev)
        except GeneratorExit:
            return
        except Exception as exc:                       # 兜底，别让连接裸断
            yield sse({'t': 'error', 'msg': f'{type(exc).__name__}: {exc}'})
        yield 'data: [DONE]\n\n'

    resp = Response(stream_with_context(gen()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache, no-transform'
    resp.headers['X-Accel-Buffering'] = 'no'
    resp.headers['Connection'] = 'keep-alive'
    return resp


@bp.post('/api/agent/upload')
def upload():
    fs = request.files.get('file')
    if not fs or not fs.filename:
        return jsonify({'ok': False, 'error': '没有收到文件'}), 400
    try:
        from agent_files import save_upload
        meta = save_upload(fs)
    except Exception as exc:
        return jsonify({'ok': False,
                        'error': f'{type(exc).__name__}: {exc}'}), 200
    return jsonify({'ok': True, 'file': meta})


@bp.get('/api/agent/file/<fid>')
def get_file(fid):
    try:
        from agent_files import serve_file
        return serve_file(fid)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 404


@bp.get('/api/agent/facts')
def get_facts():
    sid = str(request.args.get('sid') or 'default')[:64]
    d = sess_get(sid)
    return jsonify({'count': len(d['facts']),
                    'facts': [{'label': v['label'], 'digest': v['digest'],
                               'ts': v['ts']} for v in d['facts'].values()]})
