"""mt5-hedge setup: 一台机器凑够两份 MT5, 零交互.

先扫描 Program Files 下已有的 MT5(券商装的也算): 有两份 → 一个不装, 直接用; 有一份 → 当 A 腿, 再装一份当 B 腿;
一份没有 → 装两份. 自己装的 = 官方安装包静默装一次 → 复制成 MT5-A / MT5-B, 以 /portable 跑(数据在各自目录, 互不串);
已有的终端按原样跑(不加 /portable, 登录信息还在原处). 桌面放 MT5-A / MT5-B / mt5-hedge 三个快捷方式.
重复跑无害: hedge.json 里已有的腿路径只要还存在就不动.
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .paths import PKG, WORK

INSTALLER_URL = "https://raw.githubusercontent.com/cloudformer/mt5-hedge/main/installers/mt5setup.exe"
PROGRAM_FILES = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
BASE = PROGRAM_FILES / "MetaTrader 5"          # 官方安装包的默认目录
TARGETS = {"A": PROGRAM_FILES / "MT5-A", "B": PROGRAM_FILES / "MT5-B"}
SCAN_ROOTS = [PROGRAM_FILES, Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))]


def _scan() -> list:
    """机器上已有的 MT5 终端: Program Files*/<任意目录>/terminal64.exe. 自己装的 MT5-A/B 和官方默认目录不算(BASE 只是母本)."""
    found = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            exe = d / "terminal64.exe"
            if exe.exists() and d not in TARGETS.values() and d != BASE:
                found.append(exe)
    return found


def _say(msg):
    print(f"[mt5-hedge setup] {msg}", flush=True)


def _download(dst: Path):
    if dst.exists() and dst.stat().st_size > 1_000_000:
        _say(f"安装包已在 {dst}")
        return
    _say(f"下载安装包 {INSTALLER_URL}")
    urllib.request.urlretrieve(INSTALLER_URL, dst)


def _install_base(installer: Path):
    if (BASE / "terminal64.exe").exists():
        _say(f"已装: {BASE}")
        return
    _say(f"静默安装到 {BASE}(1~3 分钟, 安装器会去 MetaQuotes 拉终端本体)")
    subprocess.run([str(installer), "/auto"], check=False)
    for _ in range(120):   # /auto 可能先返回再后台装完: 等 terminal64.exe 出现
        if (BASE / "terminal64.exe").exists():
            break
        time.sleep(2)
    if not (BASE / "terminal64.exe").exists():
        raise SystemExit(f"安装后没找到 {BASE / 'terminal64.exe'}, 请手动装一次再跑 setup")
    _kill_terminal()


def _kill_terminal():
    subprocess.run(["taskkill", "/IM", "terminal64.exe", "/F"], capture_output=True)
    time.sleep(2)


def _copy_target(name: str) -> Path:
    dst = TARGETS[name]
    if (dst / "terminal64.exe").exists():
        _say(f"{name} 腿已存在: {dst}")
    else:
        _say(f"复制 {BASE} → {dst}")
        shutil.copytree(BASE, dst, ignore=shutil.ignore_patterns("logs", "*.log"))
    return dst / "terminal64.exe"


def _plan(installer: Path) -> dict:
    """决定两腿各用哪个终端. 返回 {A: {mt5_path, portable}, B: {...}}.
    顺序: 机器上本来就有的终端(人自己装的, 优先) → 以前 setup 装过的 MT5-A/B 拷贝 → 还缺的才装."""
    found = _scan()
    _say(f"扫描到已有 MT5 {len(found)} 份: " + (", ".join(str(e.parent) for e in found) or "无"))
    pool = [(e, False) for e in found] + [(d / "terminal64.exe", True) for d in TARGETS.values() if (d / "terminal64.exe").exists()]
    legs = {}
    for n in TARGETS:
        if pool:
            e, portable = pool.pop(0)
            legs[n] = {"mt5_path": str(e), "portable": portable}
    missing = [n for n in TARGETS if n not in legs]
    if missing:
        _say(f"缺 {len(missing)} 份, 安装: {', '.join(missing)}")
        _download(installer)
        _install_base(installer)
        for n in missing:
            legs[n] = {"mt5_path": str(_copy_target(n)), "portable": True}
    else:
        _say("两腿都有了, 不安装")
    return legs


def _write_cfg(legs: dict):
    from . import hedge
    for n, v in legs.items():
        v["label"] = hedge.LEGS[n].get("label", n)
    errs = hedge.save_settings(legs)
    if errs:
        raise SystemExit("写 hedge.json 失败: " + "; ".join(errs))
    for n, v in legs.items():
        _say(f"{n} 腿 = {v['mt5_path']}" + ("(便携)" if v["portable"] else "(已有安装, 原样用)"))
    _say(f"hedge.json 已写: {WORK / 'hedge.json'}")


def _shortcuts(legs: dict):
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        return
    ini = PKG / "terminal_start.ini"
    items = [(f"MT5-{n}", v["mt5_path"], ("/portable " if v["portable"] else "") + f'/config:"{ini}"',
              str(Path(v["mt5_path"]).parent)) for n, v in legs.items()]
    # 不猜 Scripts 目录: 直接用装了本包的 python 起模块, 哪个 Python 装的就用哪个
    items.append(("mt5-hedge", sys.executable, "-m mt5_hedge.cli", str(WORK)))
    ps = ["$W = New-Object -ComObject WScript.Shell"]
    for name, target, args, cwd in items:
        ps.append(f'$s = $W.CreateShortcut("{desktop / name}.lnk"); $s.TargetPath = "{target}"; '
                  f"$s.Arguments = '{args}'; $s.WorkingDirectory = \"{cwd}\"; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(ps)], capture_output=True)
    _say("桌面快捷方式: MT5-A / MT5-B(登录用, 便携模式) / mt5-hedge(启动页面)")


def main(argv=None):
    if os.name != "nt":
        raise SystemExit("setup 只在 Windows 上跑")
    legs = _plan(WORK / "mt5setup.exe")
    _write_cfg(legs)
    _shortcuts(legs)
    _say("完成. 下一步: 双击桌面 MT5-A 和 MT5-B 各登录一次账户(勾记住密码), 然后双击 mt5-hedge")


if __name__ == "__main__":
    main()
