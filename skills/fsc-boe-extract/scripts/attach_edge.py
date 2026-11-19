#!/usr/bin/env python
"""启动一个可被 CDP 接管的 Edge 实例（使用 profile 副本，不影响用户原有浏览器）。

用法:
    python attach_edge.py --port 9222
    python attach_edge.py --port 9222 --profile-src "D:/my/User Data" --work-dir "D:/tmp/edge_ctl"
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKIP_DIRS = {
    "Cache", "Code Cache", "GPUCache", "Service Worker", "DawnCache", "ShaderCache",
    "GrShaderCache", "Media Cache", "BrowserMetrics", "Crashpad",
    "component_crx_cache", "extensions_crx_cache",
}

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def port_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("找不到 msedge.exe，可用 --edge 手动指定路径")


def copy_profile(src: str, dst: str) -> None:
    if os.path.isdir(dst) and os.listdir(dst):
        print(f"[i] 复用已有 profile 副本: {dst}")
        return
    print(f"[i] 复制 profile  {src}  ->  {dst}")
    os.makedirs(dst, exist_ok=True)
    ignore = shutil.ignore_patterns(*SKIP_DIRS, "*.pma", "*.log")

    skipped = []

    def _copy(s, d):
        for item in os.listdir(s):
            if item in SKIP_DIRS:
                continue
            sp, dp = os.path.join(s, item), os.path.join(d, item)
            try:
                if os.path.isdir(sp):
                    shutil.copytree(sp, dp, ignore=ignore, dirs_exist_ok=True,
                                    ignore_dangling_symlinks=True)
                else:
                    shutil.copy2(sp, dp)
            except shutil.Error as e:
                # copytree 会汇总无法访问的单个文件（如运行中 Edge 占用的 Cookies），
                # 其余文件已复制成功，不影响使用
                for row in e.args[0]:
                    skipped.append(os.path.basename(str(row[0])))
            except Exception as e:
                skipped.append(f"{item}: {e}")

    _copy(src, dst)
    if skipped:
        uniq = sorted(set(skipped))
        print(f"[i] 有 {len(uniq)} 个文件被占用未复制（运行中的 Edge 锁定），不影响使用："
              f" {', '.join(uniq[:6])}{' ...' if len(uniq) > 6 else ''}")
    n = sum(len(f) for _, _, f in os.walk(dst))
    print(f"[i] 复制完成，{n} 个文件")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--edge", default=None, help="msedge.exe 路径")
    ap.add_argument("--profile-src", default=None, help="Edge User Data 目录")
    ap.add_argument("--work-dir", default=None, help="profile 副本存放目录")
    a = ap.parse_args()

    if port_alive(a.port):
        print(f"[✓] 端口 {a.port} 已被占用，直接复用: http://127.0.0.1:{a.port}")
        return

    src = a.profile_src or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data")
    if not os.path.isdir(src):
        raise SystemExit(f"找不到 Edge 用户数据目录: {src}")

    work = a.work_dir or os.path.join(
        os.environ.get("TEMP", "/tmp"), f"edge_cdp_{a.port}")
    copy_profile(src, work)

    exe = a.edge or find_edge()
    cmd = [
        exe,
        f"--remote-debugging-port={a.port}",
        f"--user-data-dir={work}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=msEdgeFirstRunExperience",
        "about:blank",
    ]
    print(f"[i] 启动: {' '.join(cmd[:3])} ...")
    # DETACHED_PROCESS 让浏览器脱离父进程，脚本退出后仍继续运行
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags)

    for _ in range(40):
        if port_alive(a.port):
            print(f"[✓] 就绪: http://127.0.0.1:{a.port}")
            print("[!] 若目标是需登录的系统，请让用户在新窗口手动登录（验证码无法自动化）")
            return
        time.sleep(0.5)
    raise SystemExit("[✗] 启动超时，检查是否有其他 Edge 进程占用同一 profile")


if __name__ == "__main__":
    main()
