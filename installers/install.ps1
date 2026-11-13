# mt5-hedge 一行安装(管理员 PowerShell):
#   irm https://raw.githubusercontent.com/cloudformer/mt5-hedge/main/installers/install.ps1 | iex
# 做的事: 装 Python 3.12 → pip 装 mt5-hedge → mt5-hedge setup(装两份 MT5 + 桌面快捷方式). 重复跑 = 升级.
$ErrorActionPreference = "Stop"
$Spec = if ($env:MT5_HEDGE_VERSION) { "mt5-hedge==$env:MT5_HEDGE_VERSION" } else { "mt5-hedge" }   # 固定版本: $env:MT5_HEDGE_VERSION="0.1.0"

if (-not (Get-Command python -ErrorAction SilentlyContinue) -or -not (python -c "import sys;print(sys.version_info>=(3,10) and sys.maxsize>2**32)" 2>$null) -eq "True") {
  Write-Host "[1/3] 安装 Python 3.12 (64 位)"
  winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
} else { Write-Host "[1/3] Python 已有" }

Write-Host "[2/3] 安装 $Spec (PyPI)"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet --upgrade $Spec

Write-Host "[3/3] 装两份 MT5"
mt5-hedge setup
