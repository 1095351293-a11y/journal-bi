# -*- coding: utf-8 -*-
"""脚本型技能的执行器 —— 只跑技能目录里自带的脚本

为什么要单独一层
----------------
``agent_skill`` 那套声明式技能只允许「按顺序调已注册工具」，够安全但跑不了
浏览器抓取这类活。fsc-boe-extract 这种技能必须真的起 Python 进程、开 Edge。

边界（写死在这里，不是提示词）
----------------------------
1. 只能执行 ``skills/<id>/scripts/`` 下面的文件，路径 realpath 必须落在该目录内；
2. 扩展名白名单：.py / .sh / .bat / .cmd；
3. AI **没有任何写文件工具**，所以它没法自己造一个脚本再执行——脚本只能是
   用户装进来的（仓库 skills/ 或 data/skills/）；
4. 只读模式下连这个工具都不下发（run_script 在 agent.WRITE_NAMES 里）。

长跑任务
--------
抓单据包含「等你手动扫码登录」这种阻塞，一次几分钟很正常。所以默认后台跑，
返回 job_id；用 check_script 轮询，只回显日志尾部，不会把模型卡死。
"""
import os
import re
import subprocess
import sys
import threading
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('JOURNAL_DATA_DIR') or os.path.join(BASE_DIR, 'data')
JOB_DIR = os.path.join(DATA_DIR, 'script_jobs')

ALLOWED_EXT = {'.py', '.sh', '.bat', '.cmd'}
MAX_TIMEOUT = 1800          # 30 分钟，抓几百笔单据够用
DEFAULT_TIMEOUT = 900
TAIL_CHARS = 3000

JOBS = {}                   # job_id -> 状态字典，进程重启即丢，够用
_LOCK = threading.Lock()


# ---------------------------------------------------------------- 定位
def _skill_dirs():
    """仓库里随代码发布的技能目录在前，用户目录在后（同名时用户覆盖内置）。"""
    from agent_skill import BUILTIN_DIR, SKILL_DIR
    return [BUILTIN_DIR, SKILL_DIR]


def find_skill_dir(skill_id):
    sid = re.sub(r'[^A-Za-z0-9_.-]', '_', str(skill_id or '').strip())[:60]
    if not sid:
        return None
    for root in reversed(_skill_dirs()):        # 用户目录优先
        d = os.path.join(root, sid)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, 'skill.json')):
            return d
    for root in reversed(_skill_dirs()):
        d = os.path.join(root, sid)
        if os.path.isdir(d):
            return d
    return None


def resolve(skill_id, script):
    """把 ``run_fsc.py`` 解析成绝对路径，越界一律拒绝。"""
    d = find_skill_dir(skill_id)
    if not d:
        return None, None, f'没有叫 {skill_id} 的技能目录'
    scripts_dir = os.path.realpath(os.path.join(d, 'scripts'))
    raw = str(script or '').strip().replace('\\', '/')
    if not raw:
        return None, None, '要给 script 文件名，例如 run_fsc.py'
    if raw.startswith('scripts/'):
        raw = raw[len('scripts/'):]
    p = os.path.realpath(os.path.join(scripts_dir, raw))
    if not (p == scripts_dir or p.startswith(scripts_dir + os.sep)):
        return None, None, '脚本路径越界，只允许 skills/<id>/scripts/ 下的文件'
    if not os.path.isfile(p):
        have = []
        try:
            have = sorted(os.listdir(scripts_dir))
        except OSError:
            pass
        return None, None, f'没有这个脚本：{raw}。可用：{", ".join(have) or "（空）"}'
    if os.path.splitext(p)[1].lower() not in ALLOWED_EXT:
        return None, None, f'只允许 {" / ".join(sorted(ALLOWED_EXT))} 脚本'
    return p, d, ''


def _cmd_for(path):
    ext = os.path.splitext(path)[1].lower()
    py = sys.executable or 'python'
    if ext == '.py':
        return [py, '-u', path]
    if ext == '.sh':
        return ['bash', path]
    return ['cmd', '/c', path]


# ---------------------------------------------------------------- 执行
def _env():
    e = dict(os.environ)
    e['PYTHONUNBUFFERED'] = '1'
    e['PYTHONIOENCODING'] = 'utf-8'
    e['PYTHONDONTWRITEBYTECODE'] = '1'
    e['JOURNAL_DATA_DIR'] = DATA_DIR
    return e


def _spawn(job, path, skill_dir, argv, timeout):
    cmd = _cmd_for(path) + [str(a) for a in (argv or [])]
    job['cmd'] = ' '.join(cmd)
    os.makedirs(JOB_DIR, exist_ok=True)
    with open(job['log'], 'ab') as log:
        log.write(('$ ' + job['cmd'] + '\n').encode('utf-8'))
        log.flush()
        try:
            p = subprocess.Popen(cmd, cwd=skill_dir, stdout=log,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 env=_env(), shell=False)
        except Exception as exc:                     # noqa: BLE001
            job['status'] = 'failed'
            job['error'] = f'{type(exc).__name__}: {exc}'
            job['ended'] = time.time()
            return
        job['pid'] = p.pid
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except Exception:
                pass
            code = -1
            job['error'] = f'超过 {timeout} 秒未结束，已终止'
        job['code'] = code
        job['status'] = 'done' if code == 0 else 'failed'
        job['ended'] = time.time()


def tail(job, chars=TAIL_CHARS):
    try:
        size = os.path.getsize(job['log'])
        with open(job['log'], 'rb') as f:
            f.seek(max(0, size - chars * 4))
            data = f.read()
        text = data.decode('utf-8', 'replace')
        return text[-chars:]
    except OSError:
        return ''


def start(skill_id, script, argv=None, timeout=None, background=True, sid=''):
    path, skill_dir, err = resolve(skill_id, script)
    if err:
        return {'error': err}
    # 参数里可以用 {data} 代表本系统的数据目录（绝对路径），
    # 免得模型去猜 C:\Users\... 这种路径，也不会把产物写进技能目录。
    argv = [str(a).replace('{data}', DATA_DIR).replace('${data}', DATA_DIR)
            for a in (argv or [])]
    try:
        timeout = int(timeout or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(30, min(timeout, MAX_TIMEOUT))

    jid = 'j' + uuid.uuid4().hex[:10]
    job = {'id': jid, 'skill': str(skill_id), 'script': os.path.basename(path),
           'status': 'running', 'started': time.time(), 'ended': 0,
           'code': None, 'pid': None, 'error': '', 'sid': sid,
           'log': os.path.join(JOB_DIR, jid + '.log')}
    with _LOCK:
        JOBS[jid] = job
        # 只留最近 40 个任务记录，避免内存里越堆越多
        for old in sorted(JOBS.values(), key=lambda j: j['started'])[:-40]:
            JOBS.pop(old['id'], None)

    if background:
        t = threading.Thread(target=_spawn, args=(job, path, skill_dir, argv, timeout),
                             daemon=True)
        t.start()
        time.sleep(1.2)                              # 让第一条报错能立刻回给模型
        out = {'ok': True, 'job_id': jid, 'status': job['status'], 'cmd': job.get('cmd'),
               'note': '后台运行中。用 check_script(job_id=...) 看进度和日志尾部；'
                       '脚本在等用户手动登录时，把日志里那句话原样告诉用户。'}
        if job['status'] == 'failed':
            out['error'] = job.get('error') or '启动即失败'
        out['tail'] = tail(job, 1200)
        return out

    _spawn(job, path, skill_dir, argv, timeout)
    return {'ok': job['status'] == 'done', 'job_id': jid, 'status': job['status'],
            'code': job['code'], 'error': job.get('error') or '',
            'seconds': round(job['ended'] - job['started'], 1),
            'tail': tail(job)}


def check(job_id, tail_chars=TAIL_CHARS, stop=False):
    job = JOBS.get(str(job_id or '').strip())
    if not job:
        return {'error': f'没有这个任务 {job_id}（服务重启后任务记录会清空）',
                'running': [{'job_id': j['id'], 'skill': j['skill'],
                             'script': j['script'], 'status': j['status']}
                            for j in sorted(JOBS.values(), key=lambda x: x['started'],
                                            reverse=True)[:5]]}
    if stop and job['status'] == 'running' and job.get('pid'):
        try:
            import signal
            os.kill(job['pid'], signal.SIGTERM)
        except Exception as exc:                     # noqa: BLE001
            return {'error': f'终止失败：{exc}', 'job_id': job['id']}
        job['error'] = '用户终止'
        return {'ok': True, 'job_id': job['id'], 'status': job['status'],
                'note': '已发送终止信号，稍等再查一次确认'}
    out = {'ok': job['status'] == 'done', 'job_id': job['id'], 'skill': job['skill'],
           'script': job['script'], 'status': job['status'], 'code': job['code'],
           'running_for': round((job['ended'] or time.time()) - job['started'], 1),
           'tail': tail(job, int(tail_chars) if str(tail_chars).isdigit() else TAIL_CHARS)}
    if job.get('error'):
        out['error'] = job['error']
    return out


def status():
    return [{'job_id': j['id'], 'skill': j['skill'], 'script': j['script'],
             'status': j['status'], 'code': j['code'],
             'seconds': round((j['ended'] or time.time()) - j['started'], 1)}
            for j in sorted(JOBS.values(), key=lambda x: x['started'], reverse=True)[:20]]


# ---------------------------------------------------------------- 工具
def t_run_script(args):
    from agent_perm import allow_write_tools
    if not allow_write_tools():
        return {'error': '当前是只读模式，不能执行脚本。请用户先把访问模式切到'
                         '「逐条确认」或更高。'}, []
    argv = args.get('args')
    if isinstance(argv, str):
        argv = [x for x in re.split(r'\s+', argv.strip()) if x]
    if not isinstance(argv, list):
        argv = []
    res = start(args.get('skill_id') or args.get('skill'), args.get('script'),
                argv, args.get('timeout'), bool(args.get('background', True)),
                str(args.get('__sid') or ''))
    return res, []


def t_check_script(args):
    return check(args.get('job_id'), args.get('tail_chars') or TAIL_CHARS,
                 bool(args.get('stop'))), []


TOOLS = [
    {'type': 'function', 'function': {
        'name': 'run_script',
        'description': '执行某个技能自带的脚本（只能跑 skills/<技能>/scripts/ 里的文件，'
                       '不能跑任意命令、不能写脚本）。用于浏览器抓取这类一次几分钟的活。'
                       '默认后台运行，返回 job_id，之后用 check_script 看进度。',
        'parameters': {'type': 'object', 'properties': {
            'skill_id': {'type': 'string', 'description': '技能 id，例如 fsc-boe-extract'},
            'script': {'type': 'string', 'description': '脚本文件名，例如 run_fsc.py'},
            'args': {'type': 'array', 'items': {'type': 'string'},
                     'description': '命令行参数，逐项给，例如 ["--list-url","http://..."]'},
            'background': {'type': 'boolean',
                           'description': '默认 true。会等用户操作的脚本必须用 true'},
            'timeout': {'type': 'integer',
                        'description': '秒，默认 900，最长 1800'}},
            'required': ['skill_id', 'script']}}},
    {'type': 'function', 'function': {
        'name': 'check_script',
        'description': '查看脚本任务的进度与日志尾部。任务跑完会带 exit_code 和完整日志结尾。'
                       '日志里出现「请登录」之类的话，要原样转告用户。',
        'parameters': {'type': 'object', 'properties': {
            'job_id': {'type': 'string'},
            'tail_chars': {'type': 'integer', 'description': '返回多少字符的日志尾部，默认 3000'},
            'stop': {'type': 'boolean', 'description': 'true 表示终止这个任务'}},
            'required': ['job_id']}}},
]

IMPL = {'run_script': t_run_script, 'check_script': t_check_script}
LABELS = {'run_script': '执行技能脚本', 'check_script': '查看脚本进度'}
