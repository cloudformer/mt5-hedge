"""hedge 页面 + 看护: 一个进程. 启动时按 hedge.json 拉起两条腿(leg.py A / B), 腿挂了自动重拉,
浏览器自动打开 http://127.0.0.1:<app_port>/. 逻辑复用 hedge.py(同一份配置、同一份日志 CSV).

    mt5-hedge
"""
import io
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import redirect_stdout
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import hedge
from .paths import PKG, WORK as HERE

APP_PORT = int(hedge.CFG.get("app_port", 8000))
APP_HOST = hedge.CFG.get("app_host", "0.0.0.0")
(HERE / "logs").mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(),
                              RotatingFileHandler(HERE / "logs" / "app.log", maxBytes=2_000_000,
                                                  backupCount=3, encoding="utf-8")])
log = logging.getLogger("app")
app = FastAPI(title="hedge")


# ---------- 腿看护 ----------
class LegProc:
    def __init__(self, name: str):
        self.name, self.proc, self.restarts, self.last_start = name, None, 0, 0.0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        logf = open(HERE / "logs" / f"leg_{self.name}.out", "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "mt5_hedge.leg", self.name, str(os.getpid())],
                                     cwd=str(HERE), stdout=logf, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.last_start = time.time()
        log.info("leg %s started pid=%s", self.name, self.proc.pid)

    def state(self) -> dict:
        return {"alive": self.alive(), "restarts": self.restarts,
                "pid": self.proc.pid if self.proc else None,
                "exit_code": None if self.alive() else (self.proc.returncode if self.proc else None)}


LEGPROCS = {n: LegProc(n) for n in hedge.LEGS}
STARTED_AT = datetime.now()


PUBLIC_IP = {"ip": "查询中…"}


def _fetch_public_ip():
    """启动时查一次公网 IP(换机器/换网一眼确认). 查不到就写'未知', 不影响别的."""
    import requests as rq
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            r = rq.get(url, timeout=5)
            if r.ok and r.text.strip():
                PUBLIC_IP["ip"] = r.text.strip()
                hedge.HOST["host_pub_ip"] = PUBLIC_IP["ip"]
                return
        except Exception:
            continue
    PUBLIC_IP["ip"] = "未知(无外网)"


def _lan_ip() -> str:
    try:
        import socket
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("8.8.8.8", 80))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except OSError:
        return "?"


def _close_naked():
    """唯一规则: 一对里只剩一腿(另一腿被 SL/TP 或手动平掉), 就市价平掉这一腿. 失败下一轮再试."""
    live = {}
    for name in hedge.LEGS:
        try:
            live[name] = {p["ticket"] for p in hedge.call(name, "GET", "/positions", params={"magic": hedge.MAGIC})}
        except RuntimeError:
            return   # 有腿看不见就不判断(看不见 ≠ 已平)
    for pid, tks in hedge.pairs_from_csv().items():
        alive = {leg: tk for leg, tk in tks.items() if tk in live[leg]}
        if len(tks) == 2 and len(alive) == 1:
            leg, tk = next(iter(alive.items()))
            gone_leg, gone_tk = next((l, t) for l, t in tks.items() if l != leg)
            log.warning("pair %s 单腿裸露(%s ticket %s), 补平", pid, leg, tk)
            try:
                c = hedge.call(leg, "POST", "/close", json={"ticket": tk, "magic": hedge.MAGIC})
                hedge.record(hedge.close_row(pid, leg, tk, c, "auto_naked", f"{c['result']} auto: 另一腿已出场"))
                # 先走的那腿是券商(SL/TP)或人平的: 结果和原因从券商历史拿, 补一行让这对退出活跃集合
                g = hedge.call(gone_leg, "GET", f"/realized/{gone_tk}")
                hedge.record(hedge.close_row(pid, gone_leg, gone_tk, g, g.get("reason"), "ABSENT: 券商或手动平, 由看护登记"))
                log.info("pair %s 结清: A+B 合计 %+.2f", pid,
                         sum(float(x.get("total") or 0) for x in (c, g)))
            except RuntimeError as e:
                hedge.record({"pair_id": pid, "action": "close", "leg": leg, "ticket": tk, "ok": 0,
                             "note": f"auto close failed: {e}"[:200]})
                log.error("pair %s 补平失败, 下一轮再试: %s", pid, e)


def _supervise():
    for lp in LEGPROCS.values():
        lp.start()
    while True:
        time.sleep(3)
        for lp in LEGPROCS.values():
            if not lp.alive():
                wait = min(30, 3 * (lp.restarts + 1))
                if time.time() - lp.last_start >= wait:
                    lp.restarts += 1
                    log.warning("leg %s died(exit=%s), restart #%s", lp.name, lp.proc.returncode, lp.restarts)
                    lp.start()
        try:
            _close_naked()
        except Exception:
            log.exception("close_naked crashed, continue")
        try:
            left = hedge.flush_outbox()
            if left:
                log.warning("日志待发 %s 条(log_url 不通)", left)
        except Exception:
            log.exception("flush_outbox crashed, continue")


def _stop_legs():
    for lp in LEGPROCS.values():
        if lp.alive():
            lp.proc.terminate()


# ---------- API ----------
def _run(fn, *a):
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn(*a)
        ok = True
    except SystemExit as e:
        ok = False
        if isinstance(e.code, str):
            buf.write(e.code)
    except RuntimeError as e:
        ok = False
        buf.write(str(e))
    except Exception as e:  # 页面入口不许炸: 任何未知异常也变成文字
        ok = False
        buf.write(f"内部错误: {type(e).__name__}: {e}")
        log.exception("command failed")
    return {"ok": ok, "text": buf.getvalue().strip()}


@app.get("/api/state")
def state():
    legs = {}
    for name, lc in hedge.LEGS.items():
        d = {"label": lc.get("label", name), "proc": LEGPROCS[name].state(), "positions": []}
        try:
            h = hedge.call(name, "GET", "/health")
            d.update(h)
            if "login" in h:
                d["positions"] = hedge.call(name, "GET", "/positions", params={"magic": hedge.MAGIC})
        except RuntimeError as e:
            d.update({"ok": False, "reason": str(e)})
        d["float"] = round(sum(p["profit"] + p["swap"] for p in d["positions"]), 2)
        legs[name] = d
    byticket = {n: {p["ticket"]: p for p in legs[n]["positions"]} for n in legs}
    # 目标 = 这对最近一次 open/modify 记录的 sl_usd(止损=止盈=目标)
    targets = {}
    for r in hedge.rows_this_month():
        if r["action"] in ("open", "modify") and r["ok"] == "1" and r.get("sl_usd"):
            try:
                targets[r["pair_id"]] = float(r["sl_usd"])
            except ValueError:
                pass
    pairs, seen = [], set()
    for pid, tks in hedge.pairs_from_csv().items():
        row = {"pair": pid, "legs": {}}
        for leg, tk in tks.items():
            seen.add((leg, tk))
            p = byticket.get(leg, {}).get(tk)
            row["legs"][leg] = p or {"ticket": tk, "closed": True}
        alive = [l for l, p in row["legs"].items() if not p.get("closed")]
        row["pnl"] = round(sum(p["profit"] + p["swap"] for p in row["legs"].values() if not p.get("closed")), 2)
        row["naked"] = len(alive) == 1
        row["target"] = targets.get(pid)
        # 两腿 SL/TP 美元数不一致(一边更新成功一边失败, 或一腿没挂 SL/TP) → 页面红闪
        vals = [(l, p.get("sl_usd"), p.get("tp_usd")) for l, p in row["legs"].items() if not p.get("closed")]
        row["mismatch"] = ""
        if len(vals) == 2:
            (la, sa, ta), (lb, sb, tb) = vals
            if None in (sa, ta, sb, tb):
                row["mismatch"] = "有腿缺止损或止盈: " + " / ".join(f"{l} SL ${s_} TP ${t}" for l, s_, t in vals)
            elif abs(sa - sb) > max(sa, sb) * 0.05 or abs(ta - tb) > max(ta, tb) * 0.05:
                row["mismatch"] = "两腿止损止盈不一致: " + " / ".join(f"{l} SL ${s_} TP ${t}" for l, s_, t in vals)
        pairs.append(row)
    orphans = [dict(p, leg=n) for n in legs for t, p in byticket[n].items() if (n, t) not in seen]
    first = next(iter(hedge.LEGS.values()))
    # 合约规格对比: 两家"每手每 1 价差值多少钱"不一样, 同手数就不是对冲 → 红条给出手数比例
    usym = hedge.CFG.get("symbol", "XAUUSD")
    spec_warn, specs = "", {}
    for n, lc in hedge.LEGS.items():
        bsym = lc.get("symbols", {}).get(usym)
        if bsym and "login" in legs[n]:
            try:
                sp = hedge.call(n, "GET", f"/spec/{bsym}")
                specs[n] = {"per_lot": sp["usd_per_unit_lot"], "contract": sp["contract_size"], "symbol": bsym}
            except RuntimeError:
                pass
    if len(specs) == 2:
        (na, a), (nb, b) = specs.items()
        if a["per_lot"] and b["per_lot"] and abs(a["per_lot"] - b["per_lot"]) > max(a["per_lot"], b["per_lot"]) * 0.05:
            ratio = a["per_lot"] / b["per_lot"]
            spec_warn = (f"两家合约规格不同: {legs[na]['label']} 1 手每 1 价差 ${a['per_lot']:.0f}(合约 {a['contract']:g}), "
                         f"{legs[nb]['label']} ${b['per_lot']:.0f}(合约 {b['contract']:g}) — 同手数不是对冲, "
                         f"{legs[nb]['label']} 手数应为 {legs[na]['label']} 的 {ratio:.2f} 倍")
    # 走势: 有持仓的统一品种名 → 各腿 M5 收盘 + 这些对的开仓/止损/止盈水平线
    charts = []
    sym_of = {n: {v: k for k, v in lc.get("symbols", {}).items()} for n, lc in hedge.LEGS.items()}
    held = {}
    for r in pairs:
        for n, p in r["legs"].items():
            if not p.get("closed"):
                held.setdefault(sym_of[n].get(p["symbol"], p["symbol"]), []).append((r["pair"], n, p))
    for usym, items in held.items():
        series = {}
        for n, lc in hedge.LEGS.items():
            bsym = lc.get("symbols", {}).get(usym)
            if not bsym or "login" not in legs[n]:
                continue
            try:
                series[n] = hedge.call(n, "GET", f"/rates/{bsym}", params={"n": 120})
            except RuntimeError:
                series[n] = []
        levels = [{"pair": pid, "leg": n, "side": p["side"], "open": p["price_open"], "sl": p["sl"], "tp": p["tp"]}
                  for pid, n, p in items]
        charts.append({"symbol": usym, "series": series, "levels": levels})
    closes = [r for r in hedge.rows_this_month() if r["action"] == "close" and r["ok"] == "1" and r.get("total")]
    return {"legs": legs, "pairs": pairs, "orphans": orphans, "magic": hedge.MAGIC, "charts": charts,
            "spec_warn": spec_warn, "specs": specs,
            "server": {"ip": _lan_ip(), "public_ip": PUBLIC_IP["ip"], "port": APP_PORT, "host": APP_HOST, "pid": os.getpid(),
                       "started": STARTED_AT.strftime("%m-%d %H:%M:%S")},
            "settled": {"n": len({r["pair_id"] for r in closes}), "total": round(sum(float(r["total"]) for r in closes), 2)},
            "outbox": hedge.OUTBOX.exists() and sum(1 for _ in hedge.OUTBOX.open(encoding="utf-8")) or 0,
            "log_url": bool(hedge.LOG_URL),
            "log": hedge.log_pairs(),
            "all_ok": all(l.get("ok") for l in legs.values()),
            "defaults": {"volume": hedge.CFG.get("default_volume"), "usd": hedge.CFG.get("default_usd"),
                         "symbols": list(first.get("symbols", {}))}}


class OpenReq(BaseModel):
    symbol: str
    volume: float
    usd: float          # 止损 = 止盈 = usd(对称, 一个值)
    a_side: str = "buy"


class ModifyReq(BaseModel):
    usd: float


@app.post("/api/open")
def api_open(r: OpenReq):
    if r.a_side not in ("buy", "sell"):
        return JSONResponse({"ok": False, "text": "A 方向只能 buy|sell"}, 400)
    if r.volume <= 0 or r.usd <= 0:
        return JSONResponse({"ok": False, "text": "手数 / 止损止盈 必须大于 0"}, 400)
    log.info("UI open %s", r)
    return _run(hedge.cmd_open, r.symbol, r.volume, r.usd, r.usd, r.a_side)


@app.post("/api/modify")
def api_modify(r: ModifyReq):
    if r.usd <= 0:
        return JSONResponse({"ok": False, "text": "止损止盈必须大于 0"}, 400)
    log.info("UI modify %s", r)
    return _run(hedge.cmd_modify, r.usd)


@app.post("/api/close/{target}")
def api_close(target: str):
    log.info("UI close %s", target)
    return _run(hedge.cmd_close, target)


@app.get("/api/settings")
def api_settings():
    legs = {}
    for n, lc in hedge.LEGS.items():
        legs[n] = {"label": lc.get("label", n), "mt5_path": lc.get("mt5_path", ""), "login": lc.get("login") or "",
                   "server": lc.get("server", ""), "has_password": bool(lc.get("password")),
                   "symbol": next(iter(lc.get("symbols", {}).values()), "")}
    return {"terminals": hedge.scan_terminals(), "legs": legs, "symbol": hedge.CFG.get("symbol", "XAUUSD")}


class SettingsReq(BaseModel):
    legs: dict


@app.post("/api/settings")
def api_settings_save(r: SettingsReq):
    errs = hedge.save_settings(r.legs)
    if errs:
        return JSONResponse({"ok": False, "text": "\n".join(errs)}, 400)
    log.info("settings saved; reconnecting legs")
    out = []
    for n in hedge.LEGS:
        lp = LEGPROCS[n]
        if not lp.alive():
            lp.start()
            out.append(f"{n}: 腿已启动, 正在拉起终端并登录")
            continue
        try:
            rc = hedge.call(n, "POST", "/reconnect")
            out.append(f"{n}: {'已连接' if rc['ok'] else '未连上 — ' + rc['reason']}")
        except RuntimeError as e:
            out.append(f"{n}: {e}")
    return {"ok": True, "text": "已保存\n" + "\n".join(out)}


@app.post("/api/precheck")
def api_precheck():
    """pre-check: 三项只检查只显示, 不改状态不拦按钮. 两腿就绪复用 /api/state 的判断; 远程两项走日志接口 /check."""
    st = state()
    legs = st["legs"]
    bad = [f"{l.get('label', n)}: {l.get('reason') or '未连接'}" for n, l in legs.items() if not l.get("ok")]
    items = [{"name": "两腿就绪", "ok": not bad, "text": "两腿都已连上 MT5、已登录、可交易" if not bad else "; ".join(bad)}]
    ip = PUBLIC_IP["ip"]
    if not ip or not ip[0].isdigit():
        items.append({"name": "本机时间", "ok": None, "text": "无法检查(没取到公网 IP, 没有远程结果)"})
        items.append({"name": "公网 IP 没跑过", "ok": None, "text": f"无法检查: 公网 IP {ip}"})
        return items
    local_now = datetime.now(timezone.utc)
    r = hedge.check_ip(ip)
    if r.get("server_now"):
        drift = abs((datetime.fromisoformat(r["server_now"]) - local_now).total_seconds())
        items.append({"name": "本机时间", "ok": drift <= 300,
                      "text": f"与服务器相差 {drift:.0f} 秒" + ("" if drift <= 300 else ", 超过 5 分钟, 请校准 Windows 时间")})
    else:
        items.append({"name": "本机时间", "ok": None, "text": "无法检查(接口没返回时间)"})
    items.append({"name": "公网 IP 没跑过", "ok": r["ok"], "text": f"{ip} · {r['text']}"})
    return items


@app.post("/api/leg/{name}/restart")
def api_leg_restart(name: str):
    lp = LEGPROCS.get(name)
    if not lp:
        return JSONResponse({"ok": False, "text": "没有这条腿"}, 404)
    if lp.alive():
        lp.proc.terminate()
        lp.proc.wait(10)
    lp.start()
    return {"ok": True, "text": f"腿 {name} 已重启"}


PAGE = r"""<!doctype html><html lang=zh><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Hedge</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--line:#e3e7ee;--txt:#1f2937;--mut:#6b7280;--up:#16a34a;--dn:#dc2626;--warn:#b45309;--acc:#2563eb}
*{box-sizing:border-box}body{font:14px/1.5 -apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:var(--bg);color:var(--txt)}
header{display:flex;align-items:center;gap:14px;padding:10px 20px;background:#111827;color:#fff}
header h1{font-size:16px;margin:0;font-weight:600}header .st{margin-left:auto;font-size:12px;color:#9ca3af}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#9ca3af;margin-right:6px}.dot.ok{background:var(--up)}.dot.bad{background:var(--dn)}
main{padding:16px 20px;max-width:1400px;margin:0 auto}
section{margin-bottom:18px}h2{font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin:0 0 8px;display:flex;align-items:center;gap:10px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
.card .hd{display:flex;align-items:center;gap:8px;margin-bottom:6px}.card .hd b{font-size:15px}
.tag{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}.tag.live{background:#fef2f2;color:var(--dn);border-color:#fecaca}.tag.demo{background:#ecfdf5;color:var(--up);border-color:#bbf7d0}
.kv{display:grid;grid-template-columns:auto 1fr auto 1fr;gap:2px 10px;font-size:13px}.kv span{color:var(--mut)}.kv b{font-variant-numeric:tabular-nums}
.alert{margin-top:8px;padding:6px 10px;border-radius:6px;font-size:13px;background:#fef2f2;color:var(--dn);border:1px solid #fecaca}
.alert.warn{background:#fffbeb;color:var(--warn);border-color:#fde68a}
.up{color:var(--up)}.dn{color:var(--dn)}.num{font-variant-numeric:tabular-nums;text-align:right}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:left}th{background:#f9fafb;font-weight:600;font-size:12px;color:var(--mut)}
td.num,th.num{text-align:right}tr.naked td{background:#fff1f2}tr.sub td:first-child{color:transparent}
.side{display:inline-block;padding:1px 8px;border-radius:4px;font-weight:600;font-size:12px}.side.buy{background:#dcfce7;color:#166534}.side.sell{background:#fee2e2;color:#991b1b}
button{padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:#fff;cursor:pointer;font-size:13px}button:hover{background:#f3f4f6}
button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}button.pri:hover{background:#1d4ed8}button.dng{background:#fff;color:var(--dn);border-color:#fecaca}button.dng:hover{background:#fef2f2}
button:disabled{opacity:.5;cursor:not-allowed}
form{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;align-items:end;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--mut)}input,select{padding:6px 8px;border:1px solid #d1d5db;border-radius:6px;font-size:14px}
.dir{display:flex;gap:0}.dir button{flex:1;border-radius:0}.dir button:first-child{border-radius:6px 0 0 6px}.dir button:last-child{border-radius:0 6px 6px 0}.dir button.on{background:#111827;color:#fff;border-color:#111827}
.preview{grid-column:1/-1;font-size:13px;color:var(--mut)}.preview b{color:var(--txt)}
details.pair{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:8px}details.pair>summary{cursor:pointer;padding:9px 12px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;list-style:none}details.pair>summary::-webkit-details-marker{display:none}details.pair>summary::before{content:'▶';font-size:10px;color:var(--mut)}details[open].pair>summary::before{content:'▼'}details.pair.live{border-color:var(--acc)}details.pair.warn{border-color:var(--dn)}.tag{font-size:11px;padding:1px 7px;border-radius:9px;background:#eef2ff;color:var(--acc)}.tag.done{background:#f3f4f6;color:var(--mut)}.tag.bad{background:#fee2e2;color:var(--dn)}details.pair table{border:0;border-radius:0;border-top:1px solid var(--line)}details.pair td,details.pair th{font-size:12px;padding:5px 10px}
pre{background:#0b1020;color:#d1d5db;border-radius:10px;padding:12px 14px;white-space:pre-wrap;min-height:48px;font-size:13px;margin:0}pre.ok{border-left:4px solid var(--up)}pre.bad{border-left:4px solid var(--dn)}
.muted{color:var(--mut)}
.badge{display:inline-flex;align-items:center;gap:5px;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:550;background:#fff;color:var(--mut);border:1px solid var(--line)}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.badge.ok{background:#eafaf1;color:#27ae60;border-color:#b8e6cd}.badge.warn{background:#fffbeb;color:#92400e;border-color:#fde68a}.badge.error{background:#fef2f2;color:#b91c1c;border-color:#fecaca}
.badge.blink{animation:bb 1.1s ease-in-out infinite}@keyframes bb{50%{opacity:.35}}
.banner{display:none;margin:0 0 14px;padding:10px 14px;border-radius:8px;background:#fef2f2;color:#b91c1c;border:1px solid #fecaca;font-weight:600;font-size:15px}
.banner.on{display:block;animation:bb 1.1s ease-in-out infinite}.banner.ok{display:block;background:#eafaf1;color:#27ae60;border-color:#b8e6cd;animation:none}
#health td:first-child{width:160px}#health td:nth-child(2){width:130px}
.legs{display:flex;gap:18px;flex-wrap:wrap;margin-bottom:8px}.legbox .big{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.prog{margin-top:4px}.bar{height:8px;background:#e5e7eb;border-radius:4px;overflow:hidden;margin-top:4px}.bar i{display:block;height:100%}
.setleg{display:grid;gap:8px}.setleg h4{margin:0 0 2px;font-size:13px}.setleg input,.setleg select{width:100%}
.chart{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:10px}
.chart .cap{display:flex;gap:14px;font-size:12px;color:var(--mut);margin-bottom:4px;flex-wrap:wrap}.chart .cap b{color:var(--txt)}
.sw{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:4px}
svg{width:100%;height:220px;display:block}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:900px){.two{grid-template-columns:1fr}}
.two form{height:100%}
</style>
<header><h1>Hedge · 双券商对冲</h1><span id=srv style="font-size:13px;color:#fbbf24;font-weight:600;margin-left:8px"></span><span class=st id=hdr><span class=dot></span>连接中…</span><a href="/settings" style="color:#9ca3af;font-size:13px;margin-left:14px">设置</a></header>
<main>
<div id=banner class=banner></div>
<section><h2>健康检查 <span class=muted style="text-transform:none;letter-spacing:0">可以交易才叫健康</span></h2>
<table id=health><thead><tr><th>券商<th>状态<th>详情</tr></thead><tbody id=healthRows></tbody></table>
<div style="margin-top:10px;display:flex;gap:12px;align-items:flex-start"><button type=button id=pcBtn onclick="precheck()">pre-check</button><div id=pc class=muted>开仓前点一下: 两腿就绪 · 本机时间 · 公网 IP 没跑过(只检查, 不拦)</div></div></section>
<div id=setSummary class=muted style="font-size:13px;margin-bottom:10px"></div>
<section><h2>账户</h2><div class=cards id=legs></div></section>
<div class=two><section><h2>开一对</h2>
<form id=f onsubmit="return openPair(event)">
<label>品种<select id=symbol></select></label>
<label>手数<input id=volume type=number step=0.01 min=0.01></label>
<label>止损 = 止盈 $<input id=usd type=number step=1 min=1></label>
<label>方向<div class=dir><button type=button id=dBuy class=on onclick="setDir('buy')">A 买涨 · B 买跌</button><button type=button id=dSell onclick="setDir('sell')">A 买跌 · B 买涨</button></div></label>
<label>&nbsp;<button class=pri id=openBtn>开一对</button></label>
<div class=preview id=preview></div>
</form></section><section><h2>平一对 <span class=muted id=pairsum></span><span class=muted id=settled></span><span class=dn id=outbox></span></h2>
<form onsubmit="return false" id=closeForm style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">
<div class=preview id=closePreview style="grid-column:1/-1">没有持仓对</div>
<label>止损 = 止盈 $<input id=musd type=number step=1 min=1></label>
<label>&nbsp;<button id=modBtn onclick="modify()">更新止损止盈</button></label>
<label>&nbsp;<button class=dng id=closeAll onclick="closeAll()">平一对</button></label>
</form>
</section></div><section><h2>持仓明细</h2><table style="margin-top:10px"><thead><tr><th>对<th>腿<th>ticket<th>品种<th>方向<th class=num>手数<th class=num>开仓<th class=num>现价<th class=num>止损<th class=num>止盈<th class=num>浮盈<th class=num>一对合计</tr></thead><tbody id=pairs></tbody></table></section>
<section id=chartsec style="display:none"><h2>走势 · M5 最近 10 小时 <span class=muted id=chartnote></span></h2><div id=charts></div></section>
<section><h2>日志 <span class=muted id=logsum></span></h2><pre id=out class=muted style="margin-bottom:10px">—</pre><div id=log></div></section>
</main>
<script>
const $=id=>document.getElementById(id);let dir='buy',S=null;
const money=v=>`<span class="${v>=0?'up':'dn'}">${v>=0?'+':''}${v.toFixed(2)}</span>`;
const side=s=>`<span class="side ${s}">${s==='buy'?'买涨':'买跌'}</span>`;
function setDir(d){dir=d;$('dBuy').classList.toggle('on',d==='buy');$('dSell').classList.toggle('on',d==='sell');preview();}
function preview(){if(!S)return;const sl=+$('usd').value||0,tp=sl,v=$('volume').value,sym=$('symbol').value;
 const A=S.legs.A?.label||'A',B=S.legs.B?.label||'B';const a=dir==='buy',pa=a?'买涨':'买跌',pb=a?'买跌':'买涨';
 const sp=S.specs||{};const dist=n=>sp[n]&&sp[n].per_lot?(sl/(sp[n].per_lot*v)).toFixed(2):'?';
 $('preview').innerHTML=`<b>${A}</b> ${pa} ${v} 手 ${sym}(止损距 ${dist('A')} 价位) &nbsp;+&nbsp; <b>${B}</b> ${pb} ${v} 手(止损距 ${dist('B')} 价位) &nbsp;·&nbsp; 每腿止损 $${sl} / 止盈 $${tp} &nbsp;·&nbsp; 价格走到一边止盈时另一边同价位止损, 两腿一起出场`;}
function badge(cls,txt){return `<span class="badge ${cls}${cls==='error'?' blink':''}">${txt}</span>`;}
function healthRows(s){const rows=[],probs=[];
 for(const [n,l] of Object.entries(s.legs)){const lab=l.label||n;
  if(!l.proc.alive){rows.push([lab,badge('error','腿未运行'),'3 秒内自动重拉']);probs.push(`${lab}: 腿未运行`);continue;}
  if(!('login' in l)){rows.push([lab,badge('error','未连接 MT5'),l.reason||'']);probs.push(`${lab}: ${l.reason||'未连接'}`);continue;}
  if(!l.ok){rows.push([lab,badge('error','不能交易'),`${l.reason} · 账号 ${l.login}`]);probs.push(`${lab}: ${l.reason}`);continue;}
  rows.push([lab,badge('ok','可交易'),`账号 ${l.login} · ${l.server} · ${l.demo?'DEMO':'<b class=dn>LIVE 真金</b>'} · 净值 ${l.equity.toFixed(2)} ${l.currency}`]);}
 $('healthRows').innerHTML=rows.map(r=>`<tr><td>${r[0]}<td>${r[1]}<td class=muted>${r[2]}</td></tr>`).join('');
 const mm=(s.pairs||[]).filter(r=>r.mismatch).map(r=>r.mismatch+' — 再点一次「更新止损止盈」或「平一对」');
 if(s.spec_warn)mm.push(s.spec_warn);
 const b=$('banner');if(probs.length||mm.length){b.className='banner on';b.textContent='⚠ '+[...probs.map(x=>'不能交易 — '+x),...mm].join(' ｜ ');}else{b.className='banner ok';b.textContent='✔ 两腿就绪, 可以交易';}}
function legCard(n,l){const p=l.proc;
 if(!('login' in l)){const r=l.reason||(p.alive?'腿服务启动中…':'腿进程未运行');
  return `<div class=card><div class=hd><span class="dot bad"></span><b>${l.label}</b><span class=tag>${n}</span><span style="margin-left:auto"><button onclick="restartLeg('${n}')">重启腿</button></span></div><div class=alert>${r}</div><div class=muted style="font-size:12px;margin-top:4px">腿进程 ${p.alive?'运行中 pid '+p.pid:'未运行'}${p.restarts?' · 已重启 '+p.restarts+' 次':''}</div></div>`;}
 return `<div class=card><div class=hd><span class="dot ${l.ok?'ok':'bad'}"></span><b>${l.label}</b><span class=tag>${n}</span><span class="tag ${l.demo?'demo':'live'}">${l.demo?'DEMO':'LIVE 真金'}</span><span class=muted style="margin-left:auto;font-size:12px">${l.login} · ${l.server}</span></div>
 <div class=kv><span>余额</span><b>${l.balance.toFixed(2)} ${l.currency}</b><span>净值</span><b>${l.equity.toFixed(2)}</b><span>浮动盈亏</span><b>${money(l.float)}</b><span>可用保证金</span><b>${l.margin_free.toFixed(2)}</b><span>持仓</span><b>${l.positions.length}</b><span>已用保证金</span><b>${l.margin.toFixed(2)}</b></div>
 ${l.ok?'':`<div class=alert>不能交易: ${l.reason}</div>`}</div>`;}
async function refresh(){let s;try{s=await(await fetch('/api/state')).json();}catch(e){$('hdr').innerHTML='<span class="dot bad"></span>页面服务失联';return;}
 S=s;const t=new Date().toLocaleTimeString();
 $('hdr').innerHTML=`<span class="dot ${s.all_ok?'ok':'bad'}"></span>${s.all_ok?'两腿就绪':'有腿不能交易'} · ${t}`;
 if(s.server)$('srv').textContent=`公网 IP ${s.server.public_ip} · 内网 ${s.server.ip}:${s.server.port} · 实例 PID ${s.server.pid} · 启动于 ${s.server.started}`;
 healthRows(s);
 $('legs').innerHTML=Object.entries(s.legs).map(([n,l])=>legCard(n,l)).join('');
 if(!$('symbol').options.length){s.defaults.symbols.forEach(x=>$('symbol').add(new Option(x,x)));$('volume').value=s.defaults.volume;$('usd').value=s.defaults.usd;$('musd').value=s.defaults.usd;}
 preview();
 let rows='',tot=0;
 for(const r of s.pairs){const ls=Object.keys(r.legs);tot+=r.pnl;ls.forEach((n,i)=>{const p=r.legs[n],lab=s.legs[n]?.label||n;
  rows+=`<tr class="${(r.naked||r.mismatch)?'naked':''}${i?' sub':''}"><td>${r.pair}<td>${lab}<td>${p.ticket}`+(p.closed?`<td colspan=8 class=muted>已出场(止损/止盈或手动) — 另一腿 3 秒内自动补平`:
  `<td>${p.symbol}<td>${side(p.side)}<td class=num>${p.volume}<td class=num>${p.price_open}<td class=num>${p.price_current??''}<td class=num>${p.sl}<td class=num>${p.tp}<td class=num>${money(p.profit+p.swap)}`)+
  (i?'<td>':`<td class=num rowspan=${ls.length}><b>${money(r.pnl)}</b>`)+'</tr>';});}
 for(const p of s.orphans)rows+=`<tr class=naked><td class=muted>无配对<td>${s.legs[p.leg]?.label||p.leg}<td>${p.ticket}<td>${p.symbol}<td>${side(p.side)}<td class=num>${p.volume}<td class=num>${p.price_open}<td class=num>${p.price_current??''}<td class=num>${p.sl}<td class=num>${p.tp}<td class=num>${money(p.profit+p.swap)}<td class=muted>magic ${s.magic} 但日志无记录</tr>`;
 $('pairs').innerHTML=rows||'<tr><td colspan=12 class=muted style="text-align:center;padding:18px">没有持仓对</td></tr>';
 const cur=s.pairs[0];
 if(cur){const legs=Object.entries(cur.legs).filter(([n,p])=>!p.closed);const best=legs.length?Math.max(...legs.map(([n,p])=>p.profit+p.swap)):0;const tgt=cur.target||0;const pct=tgt?Math.max(0,Math.min(100,best/tgt*100)):0;
  $('closePreview').innerHTML=`<div class=legs>${Object.entries(cur.legs).map(([n,p])=>`<div class=legbox><div class=muted>${s.legs[n]?.label||n} ${p.closed?'':side(p.side)}</div><div class=big>${p.closed?'<span class=muted>已出场</span>':money(p.profit+p.swap)}</div>${p.closed?'':`<div class=muted style="font-size:12px">止损 $${p.sl_usd??'<b class=dn>无</b>'} · 止盈 $${p.tp_usd??'<b class=dn>无</b>'}</div>`}</div>`).join('')}
   <div class=legbox><div class=muted>一对合计</div><div class=big>${money(cur.pnl)}</div></div></div>
   <div class=prog><div class=muted>目标 $${tgt||'?'} · 领先腿 ${money(best)} · 到目标 <b>${pct.toFixed(0)}%</b>${pct>=85?' <span class=up>· 差不多可以手动平了</span>':''}</div><div class=bar><i style="width:${pct}%;background:${pct>=85?'var(--up)':'var(--acc)'}"></i></div></div>`;}
 else $('closePreview').innerHTML='没有持仓对';
 $('openBtn').disabled=!s.all_ok||s.pairs.length>0;$('modBtn').disabled=!s.pairs.length;
 document.querySelector('#f .preview').innerHTML=s.pairs.length?'<span class=dn>已有一对在场, 平掉后才能再开</span>':document.querySelector('#f .preview').innerHTML;
 $('pairsum').innerHTML=s.pairs.length?`${s.pairs.length} 对 · 合计 ${money(tot)}`:'';$('closeAll').disabled=!s.pairs.length;
 $('settled').textContent=s.settled.n?` · 本月已结 ${s.settled.n} 对 合计 ${s.settled.total>=0?'+':''}${s.settled.total.toFixed(2)}`:'';
 $('outbox').textContent=s.outbox?` · 日志待发 ${s.outbox} 条(接口不通)`:(s.log_url?'':' · 未配置 log_url, 只记本地');
 drawCharts(s.charts);renderLog(s.log||[]);}
const LOGCOLS=['ts_utc','action','leg','side','volume','price','sl','tp','sl_usd','sl_prev','tp_prev','reason','retcode','ok','profit','swap','commission','total','balance_after','note'];
const touched={};
function renderLog(gs){const el=$('log');const was={};el.querySelectorAll('details').forEach(d=>was[d.dataset.pair]=d.open);
 $('logsum').textContent=gs.length?` · 共 ${gs.length} 对`:'';
 if(!gs.length){el.innerHTML='<div class=muted>还没有记录</div>';return;}
 el.innerHTML=gs.map(g=>{const open=g.pair in touched?was[g.pair]:g.active;
  const legs=Object.entries(g.legs).map(([n,l])=>`${n} ${l.side==='buy'?'买':l.side==='sell'?'卖':'?'} ${l.volume||'?'}`).join(' / ');
  const bad=g.fails>0||g.reasons.includes('auto_naked')||g.hosts.length>1;
  const t=x=>x?x.slice(5,16).replace('T',' '):'';
  return `<details class="pair ${g.active?'live':''} ${bad?'warn':''}" data-pair="${g.pair}" ${open?'open':''} ontoggle="touched['${g.pair}']=1">
   <summary><b>${g.pair}</b><span class="tag ${g.active?'':'done'}">${g.active?'在场':'已结'}</span><span class=muted>${t(g.opened)}${g.closed?' → '+t(g.closed):''}</span><span>${legs}</span>
   ${g.net!==null?`净值 ${money(g.net)}`:''}<span class=muted>${g.reasons.filter(Boolean).join(' / ')}</span>
   ${g.fails?`<span class="tag bad">失败 ${g.fails}</span>`:''}${g.reasons.includes('auto_naked')?'<span class="tag bad">代码补平</span>':''}${g.hosts.length>1?'<span class="tag bad">两腿不同机器</span>':''}
   <span class=muted style="margin-left:auto;font-size:11px">${g.hosts.join(',')} · ${Object.entries(g.legs).map(([n,l])=>`${n} ${l.login||''}`).join(' · ')}</span></summary>
   <table><thead><tr>${LOGCOLS.map(c=>`<th>${c}`).join('')}</tr></thead><tbody>${g.rows.map(r=>`<tr class="${r.ok==='0'?'naked':''}">${LOGCOLS.map(c=>`<td>${c==='ts_utc'?t(r[c]):(r[c]??'')}`).join('')}</tr>`).join('')}</tbody></table></details>`;}).join('');}
async function post(url,body){const o=$('out');o.className='';o.textContent='执行中…';document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):null});const j=await r.json();o.className=j.ok?'ok':'bad';o.textContent=(j.ok?'✔ ':'✘ ')+j.text;}
 catch(e){o.className='bad';o.textContent='✘ 页面服务失联: '+e;}
 document.querySelectorAll('button').forEach(b=>b.disabled=false);refresh();}
function openPair(e){e.preventDefault();const v=+$('volume').value,u=+$('usd').value;if(!(v>0&&u>0)){alert('手数/止损止盈必须大于 0');return false;}
 if(!confirm(`${$('preview').textContent}\n\n确认开仓?`))return false;post('/api/open',{symbol:$('symbol').value,volume:v,usd:u,a_side:dir});return false;}
function modify(){const u=+$('musd').value;if(!(u>0)){alert('止损止盈必须大于 0');return;}if(confirm('两腿止损/止盈都改成 $'+u+'?'))post('/api/modify',{usd:u});}
function closeAll(){if(confirm('平掉这一对的两腿?'))post('/api/close/all');}
async function precheck(){const b=$('pcBtn'),o=$('pc');b.disabled=true;o.className='';o.textContent='检查中…';
 try{const items=await(await fetch('/api/precheck',{method:'POST'})).json();
  o.innerHTML=items.map(i=>`<div><span class="dot ${i.ok===true?'ok':i.ok===false?'bad':''}"></span><b>${i.name}</b> <span class="${i.ok===false?'dn':'muted'}">${i.text}</span></div>`).join('')
   +`<div class=muted style="font-size:12px">${new Date().toLocaleTimeString()}</div>`;}
 catch(e){o.innerHTML='<span class=dn>pre-check 失败: 页面服务失联</span>';}
 b.disabled=false;}
function restartLeg(n){post('/api/leg/'+n+'/restart');}
const COL={A:'#2563eb',B:'#f59e0b'};
function drawCharts(cs){const sec=$('chartsec');if(!cs||!cs.length){sec.style.display='none';return;}sec.style.display='';
 $('charts').innerHTML=cs.map(c=>{const W=1000,H=220,L=8,R=70,T=10,B=22;const all=[];
  for(const n in c.series)for(const r of c.series[n])all.push(r.c);
  for(const l of c.levels){all.push(l.open);if(l.sl)all.push(l.sl);if(l.tp)all.push(l.tp);}
  if(!all.length)return `<div class=chart><div class=cap><b>${c.symbol}</b> 没有行情数据</div></div>`;
  let lo=Math.min(...all),hi=Math.max(...all);const pad=(hi-lo||1)*0.08;lo-=pad;hi+=pad;
  const N=Math.max(...Object.values(c.series).map(a=>a.length),2);
  const X=i=>L+(W-L-R)*i/(N-1),Y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo));
  const dec=(c.symbol.includes('JPY')?3:c.symbol.startsWith('XAU')?2:5);
  let g='';
  for(let k=0;k<=4;k++){const v=lo+(hi-lo)*k/4,y=Y(v);g+=`<line x1=${L} x2=${W-R} y1=${y} y2=${y} stroke="#eef0f4"/><text x=${W-R+6} y=${y+4} font-size=11 fill="#9ca3af">${v.toFixed(dec)}</text>`;}
  for(const n in c.series){const a=c.series[n];if(!a.length)continue;g+=`<polyline fill=none stroke="${COL[n]||'#888'}" stroke-width=1.6 points="${a.map((r,i)=>X(i)+','+Y(r.c)).join(' ')}"/>`;}
  const seenLv={};
  for(const l of c.levels){const lines=[[l.open,'#6b7280','开仓'],[l.sl,'#dc2626',(l.side==='buy'?'跌到':'涨到')+'止损'],[l.tp,'#16a34a',(l.side==='buy'?'涨到':'跌到')+'止盈']];
   for(const [v,col,lab] of lines){if(!v)continue;const key=v.toFixed(dec);if(seenLv[key])continue;seenLv[key]=1;const y=Y(v);
    g+=`<line x1=${L} x2=${W-R} y1=${y} y2=${y} stroke="${col}" stroke-dasharray="5 4" stroke-width=1/><text x=${L+4} y=${y-3} font-size=11 fill="${col}">${lab} ${key}</text>`;}}
  const last={};for(const n in c.series){const a=c.series[n];if(a.length)last[n]=a[a.length-1].c;}
  const t0=Object.values(c.series).find(a=>a.length);const tl=t0?new Date(t0[0].t*1000):null,tr=t0?new Date(t0[t0.length-1].t*1000):null;
  const fmt=d=>d?d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'';
  g+=`<text x=${L} y=${H-6} font-size=11 fill="#9ca3af">${fmt(tl)}</text><text x=${W-R} y=${H-6} font-size=11 fill="#9ca3af" text-anchor=end>${fmt(tr)}</text>`;
  const cap=Object.keys(c.series).map(n=>`<span><i class=sw style="background:${COL[n]||'#888'}"></i>${S.legs[n]?.label||n} <b>${last[n]!=null?last[n].toFixed(dec):'—'}</b></span>`).join('')+
   (Object.keys(last).length===2?`<span>两家价差 <b>${(last.A-last.B).toFixed(dec)}</b></span>`:'');
  return `<div class=chart><div class=cap><b>${c.symbol}</b>${cap}</div><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio=none>${g}</svg></div>`;}).join('');}
['usd','volume','symbol'].forEach(id=>$(id).addEventListener('input',preview));
async function loadSettings(){const S2=await(await fetch('/api/settings')).json();const ok=Object.values(S2.legs).every(l=>l.mt5_path);
 $('setSummary').innerHTML=ok?Object.entries(S2.legs).map(([n,l])=>`<b>${l.label}</b> ${l.mt5_path}`).join(' &nbsp;|&nbsp; ')+' &nbsp;<a href="/settings">改</a>':'<span class=dn>还没指定两个 MT5 的路径 → <a href="/settings">去设置</a></span>';}
loadSettings();refresh();setInterval(refresh,5000);
</script></html>"""


SETTINGS_PAGE = """<!doctype html><html lang=zh><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Hedge · 设置</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--line:#e3e7ee;--txt:#1f2937;--mut:#6b7280;--up:#16a34a;--dn:#dc2626;--warn:#b45309;--acc:#2563eb}
*{box-sizing:border-box}body{font:14px/1.5 -apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:var(--bg);color:var(--txt)}
header{display:flex;align-items:center;gap:14px;padding:10px 20px;background:#111827;color:#fff}
header h1{font-size:16px;margin:0;font-weight:600}header .st{margin-left:auto;font-size:12px;color:#9ca3af}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#9ca3af;margin-right:6px}.dot.ok{background:var(--up)}.dot.bad{background:var(--dn)}
main{padding:16px 20px;max-width:1400px;margin:0 auto}
section{margin-bottom:18px}h2{font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin:0 0 8px;display:flex;align-items:center;gap:10px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
.card .hd{display:flex;align-items:center;gap:8px;margin-bottom:6px}.card .hd b{font-size:15px}
.tag{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}.tag.live{background:#fef2f2;color:var(--dn);border-color:#fecaca}.tag.demo{background:#ecfdf5;color:var(--up);border-color:#bbf7d0}
.kv{display:grid;grid-template-columns:auto 1fr auto 1fr;gap:2px 10px;font-size:13px}.kv span{color:var(--mut)}.kv b{font-variant-numeric:tabular-nums}
.alert{margin-top:8px;padding:6px 10px;border-radius:6px;font-size:13px;background:#fef2f2;color:var(--dn);border:1px solid #fecaca}
.alert.warn{background:#fffbeb;color:var(--warn);border-color:#fde68a}
.up{color:var(--up)}.dn{color:var(--dn)}.num{font-variant-numeric:tabular-nums;text-align:right}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:left}th{background:#f9fafb;font-weight:600;font-size:12px;color:var(--mut)}
td.num,th.num{text-align:right}tr.naked td{background:#fff1f2}tr.sub td:first-child{color:transparent}
.side{display:inline-block;padding:1px 8px;border-radius:4px;font-weight:600;font-size:12px}.side.buy{background:#dcfce7;color:#166534}.side.sell{background:#fee2e2;color:#991b1b}
button{padding:6px 12px;border-radius:6px;border:1px solid var(--line);background:#fff;cursor:pointer;font-size:13px}button:hover{background:#f3f4f6}
button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}button.pri:hover{background:#1d4ed8}button.dng{background:#fff;color:var(--dn);border-color:#fecaca}button.dng:hover{background:#fef2f2}
button:disabled{opacity:.5;cursor:not-allowed}
form{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;align-items:end;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--mut)}input,select{padding:6px 8px;border:1px solid #d1d5db;border-radius:6px;font-size:14px}
.dir{display:flex;gap:0}.dir button{flex:1;border-radius:0}.dir button:first-child{border-radius:6px 0 0 6px}.dir button:last-child{border-radius:0 6px 6px 0}.dir button.on{background:#111827;color:#fff;border-color:#111827}
.preview{grid-column:1/-1;font-size:13px;color:var(--mut)}.preview b{color:var(--txt)}
pre{background:#0b1020;color:#d1d5db;border-radius:10px;padding:12px 14px;white-space:pre-wrap;min-height:48px;font-size:13px;margin:0}pre.ok{border-left:4px solid var(--up)}pre.bad{border-left:4px solid var(--dn)}
.muted{color:var(--mut)}
.badge{display:inline-flex;align-items:center;gap:5px;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:550;background:#fff;color:var(--mut);border:1px solid var(--line)}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.badge.ok{background:#eafaf1;color:#27ae60;border-color:#b8e6cd}.badge.warn{background:#fffbeb;color:#92400e;border-color:#fde68a}.badge.error{background:#fef2f2;color:#b91c1c;border-color:#fecaca}
.badge.blink{animation:bb 1.1s ease-in-out infinite}@keyframes bb{50%{opacity:.35}}
.banner{display:none;margin:0 0 14px;padding:10px 14px;border-radius:8px;background:#fef2f2;color:#b91c1c;border:1px solid #fecaca;font-weight:600;font-size:15px}
.banner.on{display:block;animation:bb 1.1s ease-in-out infinite}.banner.ok{display:block;background:#eafaf1;color:#27ae60;border-color:#b8e6cd;animation:none}
#health td:first-child{width:160px}#health td:nth-child(2){width:130px}
.legs{display:flex;gap:18px;flex-wrap:wrap;margin-bottom:8px}.legbox .big{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.prog{margin-top:4px}.bar{height:8px;background:#e5e7eb;border-radius:4px;overflow:hidden;margin-top:4px}.bar i{display:block;height:100%}
.setleg{display:grid;gap:8px}.setleg h4{margin:0 0 2px;font-size:13px}.setleg input,.setleg select{width:100%}
.chart{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:10px}
.chart .cap{display:flex;gap:14px;font-size:12px;color:var(--mut);margin-bottom:4px;flex-wrap:wrap}.chart .cap b{color:var(--txt)}
.sw{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:4px}
svg{width:100%;height:220px;display:block}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:900px){.two{grid-template-columns:1fr}}
.two form{height:100%}
</style>
<style>.setleg{display:grid;gap:8px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.setleg h4{margin:0 0 2px;font-size:14px}.setleg input,.setleg select{width:100%}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:900px){.grid2{grid-template-columns:1fr}}</style>
<header><h1>Hedge · 设置</h1><a href="/" style="color:#9ca3af;font-size:13px;margin-left:auto">← 返回主页</a></header>
<main>
<form id=settings onsubmit="return saveSettings(event)" style="display:block;background:none;border:0;padding:0">
 <div class=grid2><div id=setA></div><div id=setB></div></div>
 <p class=muted style="font-size:13px">两家必须是两个不同目录的 MT5。保存后腿按路径拉起终端(Algo Trading 自动开), 登录在终端里手动做一次, 终端会记住。</p>
 <button class=pri>保存并连接</button> <a href="/" style="margin-left:10px">返回主页</a>
</form>
<section style="margin-top:16px"><h2>结果</h2><pre id=out class=muted>—</pre></section>
</main>
<script>
const $=id=>document.getElementById(id);
function legForm(n,l,terms){const opts=terms.map(t=>`<option value="${t}" ${t===l.mt5_path?'selected':''}>${t}</option>`).join('');
 return `<div class=setleg><h4>券商 ${n}</h4>
 <label>名字(页面显示)<input id=lab${n} value="${l.label||''}" placeholder="如 Exness"></label>
 <label>终端(扫描到的)<select id=sel${n} onchange="$('path${n}').value=this.value"><option value="">— 选一个 —</option>${opts}</select></label>
 <label>终端路径<input id=path${n} value="${l.mt5_path||''}" placeholder="C:\\Program Files\\...\\terminal64.exe"></label>
 <label>品种名(该券商)<input id=sym${n} value="${l.symbol||''}" placeholder="XAUUSD 或 XAUUSD.m"></label></div>`;}
async function load(){const S=await(await fetch('/api/settings')).json();$('setA').innerHTML=legForm('A',S.legs.A||{},S.terminals);$('setB').innerHTML=legForm('B',S.legs.B||{},S.terminals);}
async function saveSettings(e){e.preventDefault();const legs={};for(const n of ['A','B'])legs[n]={label:$('lab'+n).value,mt5_path:$('path'+n).value.trim(),symbol:$('sym'+n).value.trim()};
 const o=$('out');o.className='';o.textContent='保存中…';
 try{const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({legs})});const j=await r.json();o.className=j.ok?'ok':'bad';o.textContent=(j.ok?'✔ ':'✘ ')+j.text+(j.ok?'\\n\\n回主页看账户卡变绿即可下单。':'');}
 catch(e){o.className='bad';o.textContent='✘ '+e;}
 load();return false;}
load();
</script></html>"""


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE


@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    return SETTINGS_PAGE


def _kill_stale_legs():
    """启动前清场: 上次没退干净的腿还占着 8001/8002 → 新腿绑不上端口死循环. 按端口找 PID 杀掉."""
    if os.name != "nt":
        return
    ports = {str(lc.get("port")) for lc in hedge.LEGS.values()}
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].rsplit(":", 1)[-1] in ports:
            pids.add(parts[4])
    for pid in pids - {str(os.getpid())}:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
        log.warning("杀掉占着腿端口的旧进程 pid=%s", pid)


def _auto_paths():
    """零配置: 两腿都没路径时, 扫到的前两个 MT5 自动分给 A、B(设置页随时可改)."""
    if any(lc.get("mt5_path") for lc in hedge.LEGS.values()):
        return
    found = hedge.scan_terminals()
    if len(found) < 2:
        log.warning("自动找终端: 只扫到 %s 个 MT5, 请到 /settings 手填路径", len(found))
        return
    legs = {n: {"mt5_path": found[i], "label": lc.get("label", n)} for i, (n, lc) in enumerate(hedge.LEGS.items())}
    errs = hedge.save_settings(legs)
    log.info("自动分配终端: %s %s", {n: v["mt5_path"] for n, v in legs.items()}, errs or "")


def main():
    threading.Thread(target=_fetch_public_ip, daemon=True).start()
    _kill_stale_legs()
    _auto_paths()
    threading.Thread(target=_supervise, daemon=True).start()
    threading.Timer(2.0, lambda: webbrowser.open(f"http://127.0.0.1:{APP_PORT}/")).start()
    urls = [f"http://127.0.0.1:{APP_PORT}/"]
    if APP_HOST == "0.0.0.0":
        try:
            import socket
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.connect(("8.8.8.8", 80))
            urls.append(f"http://{sk.getsockname()[0]}:{APP_PORT}/  (局域网)")
            sk.close()
        except OSError:
            urls.append("(局域网 IP 未知, 用 ipconfig 看)")
    else:
        urls.append("(只绑本机, 局域网不可访问)")
    log.info("hedge 页面: %s", "   ".join(urls))
    log.info("工作目录: %s", HERE)
    try:
        uvicorn.run(app, host=APP_HOST, port=APP_PORT, log_level="warning")
    finally:
        _stop_legs()


if __name__ == "__main__":
    main()
