"""mt5-hedge setup: 一台机器装两份 MT5, 零交互.

做法: 官方安装包静默装一次(默认目录) → 把安装目录复制成 MT5-A / MT5-B 两份 → 两份都以 /portable 跑
(便携模式下登录、图表、设置都存在各自目录里, 互不串; 原始安装不再用). 桌面放 MT5-A / MT5-B / mt5-hedge
三个快捷方式, 人双击 MT5-A/B 登录账户, 双击 mt5-hedge 启动页面. 重复跑无害: 已装的目录跳过.
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


def _copy_targets():
    for name, dst in TARGETS.items():
        if (dst / "terminal64.exe").exists():
            _say(f"{name} 腿已存在: {dst}")
            continue
        _say(f"复制 {BASE} → {dst}")
        shutil.copytree(BASE, dst, ignore=shutil.ignore_patterns("logs", "*.log"))


def _write_cfg():
    from . import hedge
    legs = {n: {"mt5_path": str(TARGETS[n] / "terminal64.exe"), "label": hedge.LEGS[n].get("label", n)} for n in TARGETS}
    errs = hedge.save_settings(legs)
    if errs:
        raise SystemExit("写 hedge.json 失败: " + "; ".join(errs))
    _say(f"hedge.json 已写: {WORK / 'hedge.json'}")


def _shortcuts():
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        return
    items = [(f"MT5-{n}", str(p / "terminal64.exe"), f'/portable /config:"{PKG / "terminal_start.ini"}"', str(p))
             for n, p in TARGETS.items()]
    exe = Path(sys.executable).with_name("mt5-hedge.exe")
    items.append(("mt5-hedge", str(exe), "", str(WORK)))
    ps = ["$W = New-Object -ComObject WScript.Shell"]
    for name, target, args, cwd in items:
        ps.append(f'$s = $W.CreateShortcut("{desktop / name}.lnk"); $s.TargetPath = "{target}"; '
                  f"$s.Arguments = '{args}'; $s.WorkingDirectory = \"{cwd}\"; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(ps)], capture_output=True)
    _say("桌面快捷方式: MT5-A / MT5-B(登录用, 便携模式) / mt5-hedge(启动页面)")


def main(argv=None):
    if os.name != "nt":
        raise SystemExit("setup 只在 Windows 上跑")
    installer = WORK / "mt5setup.exe"
    _download(installer)
    _install_base(installer)
    _copy_targets()
    _write_cfg()
    _shortcuts()
    _say("完成. 下一步: 双击桌面 MT5-A 和 MT5-B 各登录一次账户(勾记住密码), 然后双击 mt5-hedge")


if __name__ == "__main__":
    main()
