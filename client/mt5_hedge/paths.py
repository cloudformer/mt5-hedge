"""目录约定(全包唯一一处):
    PKG   = 包目录(代码、terminal_start.ini), pip 装到哪就在哪, 只读
    WORK  = 工作目录 %USERPROFILE%\\mt5-hedge\\ (hedge.json / hedge_YYYY-MM.csv / outbox.jsonl / logs/), 可写
环境变量 MT5_HEDGE_HOME 可改工作目录(测试用)."""
import os
from pathlib import Path

PKG = Path(__file__).resolve().parent
WORK = Path(os.environ.get("MT5_HEDGE_HOME") or (Path.home() / "mt5-hedge"))
WORK.mkdir(parents=True, exist_ok=True)
(WORK / "logs").mkdir(exist_ok=True)
