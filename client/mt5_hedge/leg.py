"""hedge 腿服务: 一条腿 = 一个 MT5 终端. 由 app.py 自动拉起, 也可手动: python -m mt5_hedge.leg A

配置来自 hedge.json 的 legs[<名>]: mt5_path(附着哪个终端), port, login/password/server(页面「设置」填).
终端没开就按路径拉起; 终端里已登录同一账号就不碰, 否则用配置登录; 连接在后台线程每 5 秒重试, /health 立刻回原因不阻塞.

端点(全部 JSON):
    GET  /health            {ok, reason, login, server, equity, trade_allowed, ...}  永不 5xx
    GET  /spec/{symbol}     合约规格 + 当前报价(美元→价距换算用)
    GET  /positions?magic=  本腿持仓
    GET  /realized/{pos}    券商侧真实结果(profit/swap/commission/total)
    GET  /rates/{symbol}    最近 n 根 M5 收盘(走势图)
    POST /open              {symbol, side, volume, sl_usd, tp_usd, magic, comment}
    POST /close             {ticket, magic}
    POST /modify            {ticket, magic, usd}  止损=止盈=usd, 以开仓价重挂
下单纪律与 mt5_factory 的 bridge/runner 同源: 必带 SL/TP, filling 自适应, retcode 原样回传.
"""
import json
import logging
import os
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import MetaTrader5 as mt5
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .paths import PKG, WORK as HERE
LEG = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LEG", "A")


def _cfg() -> dict:
    return json.loads((HERE / "hedge.json").read_text(encoding="utf-8"))["legs"][LEG]


START_INI = PKG / "terminal_start.ini"   # 拉起终端时带上: Algo Trading 开关直接开


LC = _cfg()
MT5_PATH = LC.get("mt5_path", "")
PORT = int(LC.get("port", 8001))
DEVIATION = 20

(HERE / "logs").mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(),
                              RotatingFileHandler(HERE / "logs" / f"leg_{LEG}.log", maxBytes=2_000_000,
                                                  backupCount=3, encoding="utf-8")])
log = logging.getLogger(f"leg{LEG}")

app = FastAPI(title=f"hedge-leg-{LEG}")
_lock = threading.Lock()
_reason = "尚未连接"


_launched_at = 0.0


def _terminal_running() -> bool:
    """这个路径的 terminal64.exe 在跑吗(同机两个终端同名, 必须按路径分)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-Process terminal64 -ErrorAction SilentlyContinue).Path"],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return True   # 查不了就当在跑, 交给 initialize 判断, 别重复拉起
    return any(line.strip().lower() == MT5_PATH.lower() for line in out.splitlines())


def _launch_terminal() -> None:
    """终端没开就按路径拉起(等价于双击), 登录由人在终端里做. 60s 内不重复拉."""
    global _launched_at, _reason
    if time.time() - _launched_at < 60:
        return
    _launched_at = time.time()
    try:
        args = [MT5_PATH]
        if _cfg().get("portable"):       # setup 自己装的拷贝才用便携模式(数据在安装目录); 机器上本来有的终端原样拉起, 登录信息还在原处
            args.append("/portable")
        if START_INI.exists():
            args.append(f"/config:{START_INI}")
        subprocess.Popen(args, cwd=str(Path(MT5_PATH).parent))
        log.info("terminal not running, launched %s", args)
        _reason = "已拉起终端(算法交易已开), 等它启动; 第一次请在终端里登录账户"
        time.sleep(10)
    except OSError as e:
        _reason = f"终端拉起失败: {e}"


def _connect() -> bool:
    """附着终端 + 按配置登录. 失败原因写进 _reason, 供 /health 显示.
    顺序: 先附着; 终端里已登录且账号一致 → 不碰; 否则用配置里的 login/password/server 登录."""
    global _reason, MT5_PATH
    lc = _cfg()
    MT5_PATH = lc.get("mt5_path", "")
    if not MT5_PATH:
        _reason = "未设置终端路径(页面「设置」里选)"
        return False
    if not Path(MT5_PATH).exists():
        _reason = f"终端路径不存在: {MT5_PATH}"
        return False
    _reason = "正在附着终端…"
    mt5.shutdown()
    if not mt5.initialize(path=MT5_PATH, timeout=15_000):   # 先直接附着: 开着就成功, 一个进程都不拉
        if not _terminal_running():                           # 附不上且确实没在跑, 才拉起一次
            _launch_terminal()
            if not _terminal_running():
                return False
        if not mt5.initialize(path=MT5_PATH, timeout=15_000):
            code, msg = mt5.last_error()
            _reason = f"附着终端失败({code}): {msg}. 终端还在启动? 路径对吗?"
            return False
    want = int(lc.get("login") or 0)
    a = mt5.account_info()
    if a is not None and (not want or a.login == want):
        _reason = ""
        return True
    if not want or not lc.get("password") or not lc.get("server"):
        _reason = "终端未登录, 且页面「设置」里没填账号/密码/服务器"
        return False
    if not mt5.login(want, password=lc["password"], server=lc["server"]):
        code, msg = mt5.last_error()
        _reason = f"登录失败({code}): {msg}. 账号/密码/服务器对吗?"
        return False
    a = mt5.account_info()
    if a is None or a.login != want:
        _reason = "登录后读不到账户, 稍后自动重试"
        return False
    log.info("logged in %s @ %s", a.login, a.server)
    _reason = ""
    return True


_want_reconnect = threading.Event()


def _connected_ok() -> bool:
    a = mt5.account_info()
    want = int(_cfg().get("login") or 0)
    return a is not None and (not want or a.login == want)


def _connector_loop():
    """后台连接线程: 没连上就每 5 秒试一次(拉终端/附着), 请求线程永远不在锁里等几十秒."""
    while True:
        try:
            if _want_reconnect.is_set() or not _connected_ok():
                _want_reconnect.clear()
                with _lock:
                    _connect()
        except Exception:
            log.exception("connector loop error")
        time.sleep(5)


@app.post("/reconnect")
def reconnect():
    """配置改了(路径)后由 app 调用: 让后台线程立刻按新配置重连."""
    _want_reconnect.set()
    return {"ok": _connected_ok(), "reason": _reason or "正在重连…"}


def _ensure():
    a = mt5.account_info()
    want = int(_cfg().get("login") or 0)
    if (a is None or (want and a.login != want)) and not _connect():
        raise HTTPException(503, _reason)


def _spec(symbol: str) -> dict:
    mt5.symbol_select(symbol, True)
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol) if info else None
    if info is None:
        raise HTTPException(400, f"品种 {symbol} 不存在, 检查 hedge.json 里 {LEG} 腿的品种名(市场报价里右键显示全部)")
    if tick is None or tick.ask == 0:
        raise HTTPException(400, f"{symbol} 没有报价(休市或行情未订阅)")
    per = info.trade_tick_value / info.trade_tick_size if info.trade_tick_size else 0
    return {"symbol": symbol, "digits": info.digits, "point": info.point,
            "contract_size": info.trade_contract_size, "usd_per_unit_lot": per,
            "stops_level_price": info.trade_stops_level * info.point,
            "volume_min": info.volume_min, "volume_step": info.volume_step,
            "bid": tick.bid, "ask": tick.ask, "spread_price": round(tick.ask - tick.bid, info.digits),
            "filling_mode": info.filling_mode, "_info": info}


def _filling(info):
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def _iso(ts: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")


def _pos(p) -> dict:
    d = {"ticket": p.ticket, "symbol": p.symbol,
         "side": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
         "volume": p.volume, "price_open": p.price_open, "price_current": p.price_current,
         "sl": p.sl, "tp": p.tp, "profit": p.profit, "swap": p.swap,
         "magic": p.magic, "comment": p.comment, "time": int(p.time), "open_ts": _iso(p.time),
         "sl_usd": None, "tp_usd": None, "contract_size": None}
    info = mt5.symbol_info(p.symbol)   # 把 SL/TP 折成美元, 页面比两腿是否一致
    if info:
        d["contract_size"] = info.trade_contract_size
    if info and info.trade_tick_size:
        per = info.trade_tick_value / info.trade_tick_size * p.volume
        d["sl_usd"] = round(abs(p.price_open - p.sl) * per, 2) if p.sl else None
        d["tp_usd"] = round(abs(p.tp - p.price_open) * per, 2) if p.tp else None
    return d


class OpenReq(BaseModel):
    symbol: str
    side: str
    volume: float
    sl_usd: float
    tp_usd: float
    magic: int
    comment: str = ""


class CloseReq(BaseModel):
    ticket: int
    magic: int


class ModifyReq(BaseModel):
    ticket: int
    magic: int
    usd: float      # 止损 = 止盈 = 这个美元数, 以开仓价为基准重挂


def _realized(position_id: int) -> dict:
    """券商侧真实结果: 该持仓全部 deal 的 profit/swap/commission 合计(我们不算, 终端给)."""
    deals = mt5.history_deals_get(position=position_id) or []
    a = mt5.account_info()
    base = {"profit": None, "swap": None, "commission": None, "total": None, "price_close": None,
            "balance_after": a.balance if a else None}
    if not deals:
        return base
    pr = sum(d.profit for d in deals); sw = sum(d.swap for d in deals); cm = sum(d.commission for d in deals)
    ins = [d for d in deals if d.entry == mt5.DEAL_ENTRY_IN]
    outs = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
    base.update({"profit": round(pr, 2), "swap": round(sw, 2), "commission": round(cm, 2),
                 "total": round(pr + sw + cm, 2), "price_close": outs[-1].price if outs else None})
    if ins:   # 开仓上下文, 让 close 行自己就完整
        i = ins[0]
        info = mt5.symbol_info(i.symbol)
        base.update({"symbol": i.symbol, "side": "buy" if i.type == mt5.DEAL_TYPE_BUY else "sell",
                     "volume": i.volume, "price_open": i.price, "open_ts": _iso(i.time),
                     "contract_size": info.trade_contract_size if info else None})
    if outs:   # 谁平的: 券商 SL/TP/强平, 还是人/程序
        base["reason"] = {mt5.DEAL_REASON_SL: "sl", mt5.DEAL_REASON_TP: "tp",
                          mt5.DEAL_REASON_SO: "stopout"}.get(outs[-1].reason, "manual")
    return base


@app.get("/realized/{position_id}")
def realized(position_id: int):
    """已被 SL/TP 或手动平掉的单, 看护补记时用."""
    with _lock:
        _ensure()
        return _realized(position_id)


@app.get("/health")
def health():
    if not _lock.acquire(timeout=1):    # 正在连终端(可能几十秒): 立刻回"连接中", 不让页面等
        return {"ok": False, "leg": LEG, "reason": _reason or "正在连接终端…"}
    try:
        if not _connected_ok():
            return {"ok": False, "leg": LEG, "reason": _reason or "正在连接终端…"}
        a, t = mt5.account_info(), mt5.terminal_info()
        problems = []
        if not t.connected:
            problems.append("终端与券商断线")
        if not t.trade_allowed:
            problems.append("算法交易开关未开(工具栏 Algo Trading)")
        if not a.trade_allowed:
            problems.append("账户不允许交易(只读/投资者密码)")
        return {"ok": not problems, "leg": LEG, "reason": "; ".join(problems),
                "login": a.login, "name": a.name, "server": a.server, "company": a.company, "currency": a.currency,
                "balance": a.balance, "equity": a.equity, "margin": a.margin, "margin_free": a.margin_free,
                "demo": a.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO,
                "trade_allowed": bool(t.trade_allowed), "connected": bool(t.connected)}
    finally:
        _lock.release()


@app.get("/spec/{symbol}")
def spec(symbol: str):
    with _lock:
        _ensure()
        s = _spec(symbol)
        s.pop("_info")
        return s


@app.get("/rates/{symbol}")
def rates(symbol: str, n: int = 120):
    """最近 n 根 M5(收盘价+时间), 走势图用. 没数据回空, 不报错."""
    with _lock:
        _ensure()
        mt5.symbol_select(symbol, True)
        rs = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, max(10, min(n, 500)))
        if rs is None:
            return []
        return [{"t": int(r["time"]), "c": float(r["close"])} for r in rs]


@app.get("/positions")
def positions(magic: Optional[int] = None):
    if not _lock.acquire(timeout=1):
        raise HTTPException(503, _reason or "正在连接终端…")
    try:
        if not _connected_ok():
            raise HTTPException(503, _reason or "正在连接终端…")
        ps = mt5.positions_get() or []
        return [_pos(p) for p in ps if magic is None or p.magic == magic]
    finally:
        _lock.release()


@app.post("/open")
def open_(r: OpenReq):
    with _lock:
        _ensure()
        t = mt5.terminal_info()
        if not t.connected:
            raise HTTPException(400, "终端与券商断线")
        if not t.trade_allowed:
            raise HTTPException(400, "算法交易开关未开(工具栏 Algo Trading)")
        s = _spec(r.symbol)
        info = s["_info"]
        if s["usd_per_unit_lot"] <= 0:
            raise HTTPException(400, f"{r.symbol} 合约规格异常(tick_value/tick_size=0), 无法按美元换算")
        vol = round(round(max(r.volume, info.volume_min) / info.volume_step) * info.volume_step, 8)
        if info.volume_max and vol > info.volume_max:
            raise HTTPException(400, f"手数 {vol} 超过券商上限 {info.volume_max}")
        per_unit = s["usd_per_unit_lot"] * vol
        floor = s["stops_level_price"] + s["spread_price"]
        d_sl, d_tp = r.sl_usd / per_unit, r.tp_usd / per_unit
        note = ""
        if d_sl < floor or d_tp < floor:
            note = f"券商最小止损距离 {floor:.{info.digits}f}, 已放大到该距离"
            d_sl, d_tp = max(d_sl, floor), max(d_tp, floor)
        buy = r.side.lower() == "buy"
        price = s["ask"] if buy else s["bid"]
        sl = round(price - d_sl if buy else price + d_sl, info.digits)
        tp = round(price + d_tp if buy else price - d_tp, info.digits)
        if sl <= 0 or tp <= 0 or d_sl >= price * 0.5:
            raise HTTPException(400, f"{r.sl_usd} 美元太大: {vol} 手 {r.symbol} 每 1 价差只值 {per_unit:.2f} 美元, "
                                     f"要挂到 ±{d_sl:.2f} 价位(现价 {price}), 券商不可能接受. 改小美元数或加大手数")
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": r.symbol, "volume": vol,
               "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
               "price": price, "sl": sl, "tp": tp, "deviation": DEVIATION,
               "magic": r.magic, "comment": r.comment[:31],
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(info)}
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            code = res.retcode if res else None
            msg = res.comment if res else str(mt5.last_error())
            log.error("open rejected %s %s %s: %s %s", r.symbol, r.side, vol, code, msg)
            raise HTTPException(502, f"券商拒单 retcode={code}: {msg}")
        time.sleep(0.3)
        p = next((x for x in (mt5.positions_get(symbol=r.symbol) or []) if x.ticket == res.order), None)
        if p is None:  # 有些券商 order 与 position ticket 不同: 退回按 magic+comment 找最新
            cands = [x for x in (mt5.positions_get(symbol=r.symbol) or [])
                     if x.magic == r.magic and x.comment == r.comment[:31]]
            p = max(cands, key=lambda x: x.time) if cands else None
        out = {"retcode": res.retcode, "ticket": res.order, "price": res.price, "volume": vol,
               "sl": sl, "tp": tp, "usd_per_unit": per_unit, "note": note}
        if p is not None:
            out.update(_pos(p))
            out["sl_usd"] = round(abs(p.price_open - p.sl) * per_unit, 2) if p.sl else None
            out["tp_usd"] = round(abs(p.tp - p.price_open) * per_unit, 2) if p.tp else None
            if not p.sl or not p.tp:
                out["warning"] = "持仓读回没有 SL/TP! 请立刻在终端手动补"
        else:
            out["warning"] = "下单成功但读回持仓未找到, 请在终端核对"
        log.info("open %s %s %s -> ticket %s price %s sl %s tp %s", r.symbol, r.side, vol,
                 out.get("ticket"), out.get("price_open", res.price), sl, tp)
        return out


@app.post("/close")
def close(r: CloseReq):
    with _lock:
        _ensure()
        p = next(iter(mt5.positions_get(ticket=r.ticket) or []), None)
        if p is None:
            return {"ticket": r.ticket, "result": "ABSENT", "note": "持仓不存在(已被 SL/TP 或手动平)"}
        if p.magic != r.magic:
            raise HTTPException(403, f"ticket {r.ticket} magic={p.magic} 不是 hedge 的单, 拒绝")
        info = mt5.symbol_info(p.symbol)
        tick = mt5.symbol_info_tick(p.symbol)
        if tick is None or tick.bid == 0:
            raise HTTPException(400, f"{p.symbol} 没有报价(休市?), 无法平仓")
        buy = p.type == mt5.POSITION_TYPE_BUY
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
               "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
               "price": tick.bid if buy else tick.ask, "deviation": DEVIATION,
               "magic": r.magic, "position": p.ticket, "comment": "hedge-close",
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(info)}
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            code = res.retcode if res else None
            msg = res.comment if res else str(mt5.last_error())
            log.error("close rejected %s: %s %s", p.ticket, code, msg)
            raise HTTPException(502, f"券商拒绝平仓 retcode={code}: {msg}")
        log.info("close %s -> %.5f", p.ticket, res.price)
        time.sleep(0.3)
        out = {"ticket": p.ticket, "result": "CLOSED", "price": res.price, "retcode": res.retcode,
               "sl": p.sl, "tp": p.tp, **_pos(p)}
        out.update(_realized(p.ticket))
        out["reason"] = None   # 是我们平的, 原因由调用方定(manual / auto_naked / rollback)
        return out


@app.post("/modify")
def modify(r: ModifyReq):
    with _lock:
        _ensure()
        p = next(iter(mt5.positions_get(ticket=r.ticket) or []), None)
        if p is None:
            raise HTTPException(404, f"ticket {r.ticket} 持仓不存在(已出场)")
        if p.magic != r.magic:
            raise HTTPException(403, f"ticket {r.ticket} 不是 hedge 的单, 拒绝")
        s = _spec(p.symbol)
        info = s["_info"]
        per_unit = s["usd_per_unit_lot"] * p.volume
        if per_unit <= 0:
            raise HTTPException(400, "合约规格异常, 无法换算")
        d = r.usd / per_unit
        buy = p.type == mt5.POSITION_TYPE_BUY
        sl = round(p.price_open - d if buy else p.price_open + d, info.digits)
        tp = round(p.price_open + d if buy else p.price_open - d, info.digits)
        cur = s["bid"] if buy else s["ask"]
        floor = s["stops_level_price"] + s["spread_price"]
        # 新价位不能贴着现价(券商拒), 也不能已经越过现价(等于立刻触发)
        if (buy and (sl >= cur - floor or tp <= cur + floor)) or ((not buy) and (sl <= cur + floor or tp >= cur - floor)):
            raise HTTPException(400, f"{r.usd} 美元对应的止损/止盈离现价太近或已越过(现价 {cur}, 开仓 {p.price_open}), 没改")
        res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": p.ticket,
                              "sl": sl, "tp": tp, "magic": r.magic})
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            raise HTTPException(502, f"券商拒绝修改 retcode={res.retcode if res else None}: {res.comment if res else mt5.last_error()}")
        time.sleep(0.3)
        q = next(iter(mt5.positions_get(ticket=r.ticket) or []), None)
        out = {"ticket": p.ticket, "symbol": p.symbol, "side": "buy" if buy else "sell", "volume": p.volume,
               "price_open": p.price_open, "open_ts": _iso(p.time), "price_current": cur,
               "sl_prev": p.sl, "tp_prev": p.tp, "sl": sl, "tp": tp, "retcode": res.retcode,
               "contract_size": info.trade_contract_size}
        if q is not None:
            out["sl"], out["tp"] = q.sl, q.tp
            out["sl_usd"] = round(abs(q.price_open - q.sl) * per_unit, 2) if q.sl else None
            out["tp_usd"] = round(abs(q.tp - q.price_open) * per_unit, 2) if q.tp else None
        log.info("modify %s -> sl %s tp %s (%s usd)", p.ticket, out["sl"], out["tp"], r.usd)
        return out


def _die_with_parent(ppid: int):
    """页面进程(app.py)没了就退出, 不留孤儿占端口(关黑窗口只杀 app, 腿会活着 → 下次 8001 绑不上)."""
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, ppid)   # SYNCHRONIZE
        if not h:
            return
        ctypes.windll.kernel32.WaitForSingleObject(h, 0xFFFFFFFF)       # 阻塞到父进程结束
    except Exception:
        return
    log.info("parent %s gone, leg %s exit", ppid, LEG)
    os._exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[2].isdigit():
        threading.Thread(target=_die_with_parent, args=(int(sys.argv[2]),), daemon=True).start()
    threading.Thread(target=_connector_loop, daemon=True).start()   # 连接在后台, 服务立刻可答
    log.info("leg %s up on port %s, connecting in background", LEG, PORT)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
