# mt5-hedge 一行安装(管理员 PowerShell):
#   irm https://raw.githubusercontent.com/cloudformer/mt5-hedge/main/installers/install.ps1 | iex
# 做的事: 装 Python 3.12 → pip 装 mt5-hedge → mt5-hedge setup(两份便携 MT5 + 桌面快捷方式) → 写 hedge.json.
# 重跑 = 升级. 改下面三个值就是改全网.
$ErrorActionPreference = "Stop"

# ---------------- 全网统一的三项(只在这里定义, 下面只引用) ----------------
$PKG_VERSION = "0.2.1"     # 部署的 PyPI 包版本(锁定). 临时覆盖: $env:MT5_HEDGE_VERSION="0.2.1". 版本号本身来自 mt5_hedge/__init__.py
$LOG_URL     = ""          # 来源 = terraform output log_url. 空 = 只记本地
$APP_HOST    = "0.0.0.0"   # 调试期 0.0.0.0 = 局域网可访问; 调试完改 "127.0.0.1" = 只本机
# 端口 / magic / 品种 / 默认手数 / 默认止损美元数 不在这里: 包内 DEFAULT_CFG 管; legs 由 setup 和页面写, 每台机器自己的.

$Spec = "mt5-hedge==" + $(if ($env:MT5_HEDGE_VERSION) { $env:MT5_HEDGE_VERSION } else { $PKG_VERSION })

# ---------------- 1. Python ----------------
$py = Get-Command python -ErrorAction SilentlyContinue
$ok = $py -and ((& python -c "import sys;print(sys.version_info>=(3,10) and sys.maxsize>2**32)" 2>$null) -eq "True")
if (-not $ok) {
  Write-Host "[1/4] 安装 Python 3.12 (64 位)"
  winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
} else { Write-Host "[1/4] Python 已有" }

# ---------------- 2. 包 ----------------
Write-Host "[2/4] 安装 $Spec"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet --upgrade $Spec

# ---------------- 3. 两份 MT5 + 桌面快捷方式 ----------------
Write-Host "[3/4] 装两份 MT5"
mt5-hedge setup

# ---------------- 4. hedge.json: 只覆盖 log_url / app_host 两个键, 其余(legs, 页面设置)原样保留 ----------------
# 用 Python 合并: PowerShell 的 ConvertTo-Json 默认只两层会截断 legs, 且 PowerShell 5 写 UTF-8 带 BOM.
Write-Host "[4/4] 写 hedge.json"
$env:MT5_HEDGE_LOG_URL = $LOG_URL
$env:MT5_HEDGE_APP_HOST = $APP_HOST
python -c @"
import json, os
from mt5_hedge.paths import WORK
f = WORK / 'hedge.json'
cfg = json.loads(f.read_text(encoding='utf-8-sig')) if f.exists() else {}
cfg['log_url'] = os.environ['MT5_HEDGE_LOG_URL']
cfg['app_host'] = os.environ['MT5_HEDGE_APP_HOST']
f.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('hedge.json:', f, '| log_url:', cfg['log_url'] or '(空, 只记本地)', '| app_host:', cfg['app_host'])
"@
Write-Host "完成. 双击桌面 MT5-A / MT5-B 各登录一次账户, 然后双击 mt5-hedge"
