"""hedge 命令行: 一对 = A 腿一张单 + B 腿一张反向单, 同手数(按 volume_ratio)同美元 SL/TP.

    mt5-hedge status                         两腿持仓 + 配对 + 净敞口
    mt5-hedge open XAUUSD [0.10] [--usd 500] [--a buy|sell]   止损=止盈=usd
    mt5-hedge close <pair_id> | all          平一对
    mt5-hedge modify 600                     在场那对两腿止损=止盈=600 美元
    mt5-hedge health                         两腿账户/开关

配置默认值在 DEFAULT_CFG, hedge.json 是页面写的运行时状态. 日志 hedge_YYYY-MM.csv 只增不改, 成功失败都记;
配对关系以终端里现在的持仓为准(pairs_live): 页面/平仓/改单都读终端, 日志只是流水和补充线索.
"""
import csv
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from urllib.parse import urlencode

from .paths import PKG, WORK as HERE   # 代码在包目录, 数据都在工作目录
DEFAULT_CFG = {   # 唯一默认值; hedge.json 是运行时状态(页面改, 人不用手改), 没有就按这个生成
    "magic": 999998, "symbol": "XAUUSD", "default_volume": 0.10, "default_usd": 500,
    "app_port": 8000, "app_host": "127.0.0.1",   # 兜底只本机; 真正取值由 installers/install.ps1 统一写进 hedge.json
    "log_url": "",     # 收日志接口(aws/terraform 输出的 log_url); 空 = 只记本地
    # portable: True = setup 自己装的拷贝, 以 /portable 拉起(数据在安装目录); False = 机器上本来就有的终端, 原样拉起
    "legs": {"A": {"label": "券商1", "mt5_path": "", "portable": False, "port": 8001, "login": 0, "password": "", "server": "",
                   "symbols": {"XAUUSD": "XAUUSD"}, "volume_ratio": 1.0},
             "B": {"label": "券商2", "mt5_path": "", "portable": False, "port": 8002, "login": 0, "password": "", "server": "",
                   "symbols": {"XAUUSD": "XAUUSD"}, "volume_ratio": 1.0}}}


def _load_cfg() -> dict:
    f = HERE / "hedge.json"
    if not f.exists():
        f.write_text(json.dumps(DEFAULT_CFG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        cfg = json.loads(f.read_text(encoding="utf-8-sig"))
    except ValueError as e:   # 文件写坏了: 报错停下指出位置, 绝不覆盖人改的文件
        raise SystemExit(f"hedge.json 第 {e.lineno} 行第 {e.colno} 列格式不对: {e.msg}\n"
                         f"常见原因: 少逗号 / 多逗号 / 引号或句号打成了中文. 改好再启动.")
    for k, v in DEFAULT_CFG.items():          # 缺的键补默认(升级后老文件也能用)
        cfg.setdefault(k, v if not isinstance(v, dict) else json.loads(json.dumps(v)))
    for n, d in DEFAULT_CFG["legs"].items():
        lc = cfg["legs"].setdefault(n, {})
        for k, v in d.items():
            lc.setdefault(k, v)
    return cfg


CFG = _load_cfg()
MAGIC = int(CFG["magic"])
LEGS = CFG["legs"]
for _n, _l in LEGS.items():
    _l.setdefault("url", f"http://127.0.0.1:{_l.get('port', 8001)}")
    _l.setdefault("label", _n)
TIMEOUT = 20          # 下单/平仓/改单
READ_TIMEOUT = 5      # 页面轮询(health/positions/rates): 腿忙就当暂时不可用, 不拖页面

# 日志表 hedge_log: 一腿一动作一行, 每行自带全部上下文(机器/账户/合约/开仓价), 读哪行都不用看别的行.
# 一对走完 = 4 行: open A / open B / close A / close B; modify 可选, 改几次几行. pair_id 是唯一分组键.
# key = 幂等主键(重发覆盖): pair_id_ticket_action, modify 多次再加时间; 对方 INSERT ... ON CONFLICT (key) DO UPDATE.
CSV_FIELDS = ["key", "ts_utc", "pair_id", "action", "leg", "side", "symbol", "volume", "ticket",
              "open_price", "open_ts", "price", "sl", "tp", "sl_usd", "tp_usd", "sl_prev", "tp_prev",
              "reason", "retcode", "ok", "profit", "swap", "commission", "total", "balance_after",
              "broker", "company", "server", "login", "account", "currency", "contract_size",
              "host_name", "host_lan_ip", "host_pub_ip", "app_pid", "app_started", "note"]
LOG_URL = CFG.get("log_url", "")
LOG_TABLE = "hedge_log"   # 接口按这个字段选表
# 请求签名钥匙: 随 PyPI 包带着(mt5_hedge/sign_key.txt), terraform 读同一文件给接口. 用户不配置.
SIGN_KEY = (PKG / "sign_key.txt").read_text(encoding="utf-8").strip().encode()
OUTBOX = HERE / "outbox.jsonl"


def _lan_ip() -> str:
    try:
        import socket
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("8.8.8.8", 80))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except OSError:
        return ""


import socket as _socket
HOST = {   # 本机信息, 每行日志都带(同一次启动所有行相同; 两腿不同 = 异常). host_pub_ip 由 app.py 查到后填
    "host_name": _socket.gethostname(), "host_lan_ip": _lan_ip(), "host_pub_ip": "",
    "app_pid": os.getpid(), "app_started": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# ---------- 日志: 本地 CSV(缓存) + POST 到 log_url(日志) ----------
def signed_post(path: str, form: dict, timeout: int = 10) -> requests.Response:
    """表单 POST 到接口, 头 X-Sign = HMAC-SHA256(钥匙, 请求体). 不签时间: 机器时间不准也不能丢日志."""
    body = urlencode(form)
    sig = hmac.new(SIGN_KEY, body.encode(), hashlib.sha256).hexdigest()
    return requests.post(LOG_URL.rstrip("/") + path, data=body, timeout=timeout,
                         headers={"content-type": "application/x-www-form-urlencoded", "x-sign": sig})


def _post_log(row: dict) -> bool:
    """发一条; 2xx 才算记上. 字段名 = CSV 列名 = 表列名, 对方按 key 覆盖."""
    if not LOG_URL:
        return True
    try:   # 日志发送绝不能抛: 这函数跑在下单之后, 抛了就会让另一腿不开
        data = {k: ("" if row.get(k) is None else row.get(k)) for k in CSV_FIELDS}
        data["table"] = LOG_TABLE
        r = signed_post("/", data)
        if 400 <= r.status_code < 500:   # 格式被拒: 重发也不会通过, 记下原因不进队列
            print(f"!! 日志被接口拒绝({r.status_code}): {r.text[:200]}")
            return True
        return 200 <= r.status_code < 300
    except Exception:
        return False


def check_ip(ip: str) -> dict:
    """pre-check 远程项: 这个公网 IP 跑过没有. 只读幂等. 返回 {ok, text, server_now}."""
    if not LOG_URL:
        return {"ok": None, "text": "未配置 log_url, 无法检查", "server_now": None}
    try:
        r = signed_post("/check", {"host_pub_ip": ip}, timeout=10)
    except requests.RequestException as e:
        return {"ok": None, "text": f"接口不通: {e.__class__.__name__}", "server_now": None}
    try:
        j = r.json()
    except ValueError:
        j = {}
    now = j.get("now")
    if r.status_code == 200:
        return {"ok": True, "text": "公网 IP 没跑过", "server_now": now}
    if r.status_code == 409:
        return {"ok": False, "text": f"公网 IP {ip} 已经跑过", "server_now": now}
    return {"ok": None, "text": f"接口返回 {r.status_code}: {r.text[:120]}", "server_now": now}


_ACCT: dict = {}   # leg -> {broker, login, account, company} 本次进程内缓存(health 拿到一次就够)


def acct(leg: str) -> dict:
    if leg not in _ACCT:
        try:
            h = call(leg, "GET", "/health")
        except RuntimeError:
            h = {}
        if "login" in h:
            _ACCT[leg] = {"broker": LEGS[leg].get("label", leg), "login": h["login"],
                          "account": h.get("name", ""), "company": h.get("company", ""),
                          "server": h.get("server", ""), "currency": h.get("currency", "")}
        else:
            return {"broker": LEGS[leg].get("label", leg)}
    return _ACCT[leg]


def _log_key(row: dict) -> str:
    """幂等主键. 没 ticket(下单被拒)或 modify(可多次)时加时间, 保证一行一键."""
    k = f"{row['pair_id']}_{row.get('ticket') or row['leg']}_{row['action']}"
    if row["action"] == "modify" or not row.get("ticket"):
        k += f"_{row['ts_utc']}"
    return k


def _csv_file() -> Path:
    """本月 CSV; 表头是老版本的就先按新列重写一遍(老行缺的列留空, 内容不丢)."""
    f = HERE / f"hedge_{datetime.now(timezone.utc):%Y-%m}.csv"
    if f.exists():
        with f.open(newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            if rd.fieldnames != CSV_FIELDS:
                rows = list(rd)
                with f.open("w", newline="", encoding="utf-8") as out:
                    w = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
                    w.writeheader()
                    for r in rows:
                        r.setdefault("key", _log_key(r) if r.get("pair_id") and r.get("action") and r.get("ts_utc") else "")
                        w.writerow(r)
    return f


def record(row: dict):
    who = acct(row["leg"]) if row.get("leg") in LEGS else {}
    row = {"ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **HOST, **who, **row}
    row["key"] = _log_key(row)
    f = _csv_file()
    new = not f.exists()
    with f.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)
    if not _post_log(row):
        try:
            with OUTBOX.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        print(f"!! 日志未发出(log_url 不通), 已进待发队列: {row.get('action')} {row.get('leg')} {row.get('ticket')}")


def flush_outbox() -> int:
    """重发待发队列; 全部成功才清空, 否则留下没发出去的. 返回剩余条数."""
    if not LOG_URL or not OUTBOX.exists():
        return 0
    rows = [json.loads(x) for x in OUTBOX.read_text(encoding="utf-8").splitlines() if x.strip()]
    left = [r for r in rows if not _post_log(r)]
    if left:
        OUTBOX.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in left), encoding="utf-8")
    else:
        OUTBOX.unlink(missing_ok=True)
    return len(left)


def settle(pid: str, results: dict):
    """一对两腿都出场后只打印合计(不记行、不外发: 日志就是 4 条 开A/开B/平A/平B, 合计由读的人加)."""
    tot = sum(float(r.get("total") or 0) for r in results.values())
    missing = [l for l, r in results.items() if r.get("total") is None]
    print(f"pair {pid} 结清: A+B 合计 {tot:+.2f}" + (f"  (缺券商结果: {','.join(missing)})" if missing else ""))


def rows_this_month() -> list:
    f = HERE / f"hedge_{datetime.now(timezone.utc):%Y-%m}.csv"
    if not f.exists():
        return []
    with f.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def rows_all() -> list:
    """全部月份的日志行, 按文件顺序(=时间顺序). 页面「日志」区用."""
    out = []
    for f in sorted(HERE.glob("hedge_*.csv")):
        with f.open(newline="", encoding="utf-8") as fh:
            out.extend(csv.DictReader(fh))
    return out


def log_pairs(active=None) -> list:
    """按 pair 分组, 最新在前. 每组: 概要(状态/时间/两腿/净值/原因/机器) + 原始行. 纯读 CSV, 不算不猜.
    active = 在场的 pair 集合(调用方从终端取 pairs_live); 不传就按日志算."""
    groups: dict = {}
    for r in rows_all():
        if r.get("pair_id"):
            groups.setdefault(r["pair_id"], []).append(r)
    if active is None:
        active = pairs_from_csv()
    out = []
    for pid, rows in groups.items():
        opens = [r for r in rows if r["action"] == "open" and r.get("ok") == "1"]
        closes = [r for r in rows if r["action"] == "close" and r.get("ok") == "1"]
        tot = [float(r["total"]) for r in closes if r.get("total") not in (None, "")]
        out.append({
            "pair": pid, "active": pid in active,
            "opened": min((r["ts_utc"] for r in rows), default=""),
            "closed": max((r["ts_utc"] for r in closes), default="") if closes and pid not in active else "",
            "legs": {r["leg"]: {"side": r.get("side"), "volume": r.get("volume"), "symbol": r.get("symbol"),
                                "login": r.get("login"), "broker": r.get("broker")} for r in opens},
            "net": round(sum(tot), 2) if tot else None,
            "reasons": [r.get("reason") or "" for r in closes],
            "fails": sum(1 for r in rows if r.get("ok") == "0"),
            "hosts": sorted({r.get("host_name") or "" for r in rows} - {""}),
            "rows": rows})
    out.sort(key=lambda g: g["opened"], reverse=True)
    return out


def pairs_from_csv() -> dict:
    """pair_id -> {leg: ticket} 只取 open 成功的行; close 成功的行把 ticket 从活跃集合里拿掉."""
    pairs: dict = {}
    for f in sorted(HERE.glob("hedge_*.csv")):
        with f.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["ok"] != "1" or not r["ticket"]:
                    continue
                if r["action"] == "open":
                    pairs.setdefault(r["pair_id"], {})[r["leg"]] = int(r["ticket"])
                elif r["action"] == "close":
                    pairs.get(r["pair_id"], {}).pop(r["leg"], None)
    return {k: v for k, v in pairs.items() if v}


def pairs_live(positions: dict | None = None) -> dict:
    """pair_id -> {leg: ticket}, 唯一口径: 终端里现在有的、带本程序 magic 的持仓. 日志不在本机也能配.
    配对线索按可信度: ① 开仓时写进单子的 comment 'hedge-<pid>'(本程序写的结构化标记, 不是人填的)
    ② 本机日志里这张 ticket 的 pair_id ③ 都没有(券商改写了 comment、日志不在本机): A/B 各剩的反向单按开仓时间
    就近配, pair_id = 较早一张的开仓时间(同样的持仓每次算出来都一样, 平仓/日志不乱).
    positions 省略就现从两腿取(腿不通抛 RuntimeError)."""
    if positions is None:
        positions = {leg: call(leg, "GET", "/positions", params={"magic": MAGIC}) for leg in LEGS}
    bylog = {}
    for f in sorted(HERE.glob("hedge_*.csv")):
        with f.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r["action"] == "open" and r["ok"] == "1" and r["ticket"]:
                    bylog[(r["leg"], int(r["ticket"]))] = r["pair_id"]
    pairs: dict = {}
    rest = {leg: [] for leg in positions}
    for leg, ps in positions.items():
        for q in ps:
            c = q.get("comment") or ""
            pid = c[6:] if c.startswith("hedge-") and len(c) > 6 else bylog.get((leg, q["ticket"]))
            if pid and leg not in pairs.get(pid, {}):
                pairs.setdefault(pid, {})[leg] = q["ticket"]
            else:
                rest[leg].append(q)
    legs = list(positions)
    if len(legs) == 2:
        a, b = legs
        for pa in sorted(rest[a], key=lambda q: q["time"]):
            pb = next((q for q in sorted(rest[b], key=lambda q: q["time"]) if q["side"] != pa["side"]), None)
            if pb is None:
                continue
            rest[b].remove(pb)
            pid = datetime.fromtimestamp(min(pa["time"], pb["time"]), timezone.utc).strftime("%m%d-%H%M%S")
            while pid in pairs:
                pid += "x"
            pairs[pid] = {a: pa["ticket"], b: pb["ticket"]}
    return pairs


# ---------- 设置(页面「设置」卡用) ----------
def scan_terminals() -> list:
    """扫机器上装的 MT5: Program Files*/ 下两层内的 terminal64.exe."""
    roots = [Path(p) for p in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                                os.environ.get("LOCALAPPDATA", "")) if p]
    found = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for d in root.iterdir():
                exe = d / "terminal64.exe"
                if exe.exists():
                    found.append(str(exe))
        except OSError:
            continue
    return sorted(set(found))


def save_settings(legs: dict) -> list:
    """legs = {A: {mt5_path, login, password, server, symbol, label}, B: {...}}. 校验后写回 hedge.json. 返回错误列表."""
    global CFG, LEGS
    errs = []
    paths = [v.get("mt5_path", "") for v in legs.values()]
    if len(set(p for p in paths if p)) < len([p for p in paths if p]):
        errs.append("两腿不能选同一个终端")
    for n, v in legs.items():
        v.setdefault("mt5_path", LEGS.get(n, {}).get("mt5_path", ""))
        if not v.get("mt5_path"):
            errs.append(f"{n} 腿没选终端路径")
        elif not Path(v["mt5_path"]).exists():
            errs.append(f"{n} 腿路径不存在: {v['mt5_path']}")
        if v.get("login") and not str(v["login"]).isdigit():
            errs.append(f"{n} 腿账号必须是数字")
    if errs:
        return errs
    cfg = json.loads((HERE / "hedge.json").read_text(encoding="utf-8"))
    for n, v in legs.items():
        lc = cfg["legs"].setdefault(n, {"port": 8001 if n == "A" else 8002})
        if v["mt5_path"] != lc.get("mt5_path"):   # 换了终端: 便携与否由调用方说(setup), 页面换路径默认按已有安装(原样拉起)
            lc["portable"] = bool(v.get("portable", False))
        elif "portable" in v:
            lc["portable"] = bool(v["portable"])
        lc["mt5_path"] = v["mt5_path"]
        lc["label"] = v.get("label") or lc.get("label") or n
        if "login" in v:               # 页面不再填账号(手动登录); 留给手改 hedge.json 的人
            lc["login"] = int(v["login"]) if v.get("login") else 0
        if v.get("password"):
            lc["password"] = v["password"]
        if "server" in v:
            lc["server"] = v["server"]
        sym = (v.get("symbol") or "").strip()
        if sym:
            lc["symbols"] = {cfg.get("symbol", "XAUUSD"): sym}
    (HERE / "hedge.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    CFG = cfg
    LEGS = cfg["legs"]
    for _n, _l in LEGS.items():
        _l.setdefault("url", f"http://127.0.0.1:{_l.get('port', 8001)}")
        _l.setdefault("label", _n)
    return []


# ---------- 腿调用 ----------
def call(leg: str, method: str, path: str, **kw):
    url = LEGS[leg]["url"].rstrip("/") + path
    try:
        r = requests.request(method, url, timeout=READ_TIMEOUT if method == "GET" else TIMEOUT, **kw)
    except requests.ConnectionError as e:
        raise RuntimeError(f"[{leg}] 腿服务没起来({url})") from e
    except requests.Timeout as e:
        raise RuntimeError(f"[{leg}] 腿服务无响应, 终端可能正在启动或卡住") from e
    except requests.RequestException as e:
        raise RuntimeError(f"[{leg}] 请求失败: {e}") from e
    if r.status_code >= 400:
        try:
            d = r.json().get("detail")
        except ValueError:
            d = r.text
        raise RuntimeError(f"[{leg}] HTTP {r.status_code}: {d}")
    return r.json()


def leg_symbol(leg: str, symbol: str) -> str:
    m = LEGS[leg].get("symbols", {})
    if symbol not in m:
        raise RuntimeError(f"[{leg}] hedge.json 没有 {symbol} 的品种映射")
    return m[symbol]


def leg_volume(leg: str, volume: float) -> float:
    return round(volume * float(LEGS[leg].get("volume_ratio", 1.0)), 2)


# ---------- 命令 ----------
def cmd_health():
    for leg in LEGS:
        try:
            h = call(leg, "GET", "/health")
            if "login" not in h:
                print(f"{leg}: 未连接 — {h['reason']}")
                continue
            flag = "" if h["ok"] else f"  <-- 不能交易: {h['reason']}"
            print(f"{leg}: login={h['login']} {h['server']} {'DEMO' if h['demo'] else 'LIVE'} "
                  f"equity={h['equity']} {h['currency']}{flag}")
        except RuntimeError as e:
            print(f"{leg}: {e}")


def cmd_status():
    raw = {leg: call(leg, "GET", "/positions", params={"magic": MAGIC}) for leg in LEGS}
    pairs = pairs_live(raw)
    live = {leg: {p["ticket"]: p for p in ps} for leg, ps in raw.items()}
    seen = set()
    print(f"{'pair':<16}{'leg':<4}{'ticket':<12}{'symbol':<10}{'side':<5}{'vol':>6}{'open':>11}{'sl':>11}{'tp':>11}{'pnl':>9}")
    for pid, legs in pairs.items():
        for leg, tk in legs.items():
            p = live[leg][tk]
            seen.add((leg, tk))
            print(f"{pid:<16}{leg:<4}{tk:<12}{p['symbol']:<10}{p['side']:<5}{p['volume']:>6}"
                  f"{p['price_open']:>11.{3}f}{p['sl']:>11.3f}{p['tp']:>11.3f}{p['profit'] + p['swap']:>9.2f}")
        alive = [l for l, tk in legs.items() if tk in live[l]]
        if len(alive) == 1:
            print(f"{'':<16}!! 单腿裸露: 只剩 {alive[0]} 腿, 另一腿已出场 —— python hedge.py close {pid}")
    orphans = [(l, p) for l in live for t, p in live[l].items() if (l, t) not in seen]
    for l, p in orphans:
        print(f"{'(无配对)':<16}{l:<4}{p['ticket']:<12}{p['symbol']:<10}{p['side']:<5}{p['volume']:>6}  magic={MAGIC} 但另一腿没有反向单")
    net = {}
    for l in live:
        for p in live[l].values():
            net[p["symbol"]] = net.get(p["symbol"], 0) + (p["volume"] if p["side"] == "buy" else -p["volume"])
    print("净敞口(手, 按各腿品种名):", {k: round(v, 2) for k, v in net.items()} or "0")


def cmd_open(symbol: str, volume: float, sl_usd: float, tp_usd: float, a_side: str):
    active = pairs_live()
    if active:   # 一次只允许一对
        raise SystemExit(f"已有一对在场({', '.join(active)}), 先平掉再开")
    pid = datetime.now(timezone.utc).strftime("%m%d-%H%M%S")
    b_side = "sell" if a_side == "buy" else "buy"
    plan = [("A", a_side), ("B", b_side)]
    hs = {leg: call(leg, "GET", "/health") for leg in LEGS}
    for leg, h in hs.items():
        if not h["ok"]:
            raise SystemExit(f"[{leg}] 不能交易: {h['reason']} — 一腿都没下")
    done = []
    for leg, side in plan:
        sym, vol = leg_symbol(leg, symbol), leg_volume(leg, volume)
        try:
            r = call(leg, "POST", "/open", json={"symbol": sym, "side": side, "volume": vol,
                                                  "sl_usd": sl_usd, "tp_usd": tp_usd,
                                                  "magic": MAGIC, "comment": f"hedge-{pid}"})
        except RuntimeError as e:
            record({"pair_id": pid, "action": "open", "leg": leg, "login": hs[leg].get("login"),
                   "symbol": sym, "side": side, "volume": vol, "ok": 0, "note": str(e)[:200]})
            print(f"[{leg}] 开仓失败: {e}")
            # 回滚已开的腿, 不留单腿裸露
            for dleg, dr in done:
                try:
                    c = call(dleg, "POST", "/close", json={"ticket": dr["ticket"], "magic": MAGIC})
                    record({"pair_id": pid, "action": "close", "leg": dleg, "login": hs[dleg]["login"],
                           "symbol": dr["symbol"], "side": dr["side"], "volume": dr["volume"],
                           "ticket": dr["ticket"], "price": c.get("price"), "ok": 1, "reason": "rollback",
                           "open_price": c.get("price_open"), "open_ts": c.get("open_ts"),
                           "contract_size": c.get("contract_size"), "profit": c.get("profit"), "swap": c.get("swap"),
                           "commission": c.get("commission"), "total": c.get("total"),
                           "balance_after": c.get("balance_after"), "note": "rollback: 另一腿开仓失败"})
                    print(f"[{dleg}] 已回滚平仓 ticket {dr['ticket']} @ {c.get('price')}")
                except RuntimeError as e2:
                    record({"pair_id": pid, "action": "close", "leg": dleg, "ticket": dr["ticket"],
                           "ok": 0, "note": f"rollback failed: {e2}"[:200]})
                    print(f"!! [{dleg}] 回滚失败, ticket {dr['ticket']} 裸露, 立刻手动处理: {e2}")
            raise SystemExit(1)
        done.append((leg, r))
        record({"pair_id": pid, "action": "open", "leg": leg, "login": hs[leg]["login"],
               "symbol": sym, "side": side, "volume": r["volume"], "ticket": r["ticket"],
               "open_price": r.get("price_open", r["price"]), "open_ts": r.get("open_ts"),
               "price": r.get("price_open", r["price"]), "sl": r["sl"], "tp": r["tp"],
               "sl_usd": r.get("sl_usd"), "tp_usd": r.get("tp_usd"), "contract_size": r.get("contract_size"),
               "retcode": r["retcode"], "ok": 1, "note": (r.get("warning") or r.get("note") or "")})
    print(f"pair {pid} 开好:")
    for leg, r in done:
        bad = ""
        for k, want in (("sl_usd", sl_usd), ("tp_usd", tp_usd)):
            got = r.get(k)
            if got is None or abs(got - want) > want * 0.05:
                bad += f"  <-- {k} 实际 {got} 偏离要求 {want}"
        extra = ("  !! " + r["warning"]) if r.get("warning") else ("  (" + r["note"] + ")" if r.get("note") else "")
        print(f"  {leg} {r['symbol']} {'买涨' if r['side']=='buy' else '买跌'} {r['volume']} @ {r.get('price_open', r['price'])} "
              f"SL {r['sl']}(${r.get('sl_usd')}) TP {r['tp']}(${r.get('tp_usd')}) ticket {r['ticket']}{bad}{extra}")


def cmd_modify(usd: float):
    """把在场那一对两腿的止损/止盈都改成 usd 美元(以各自开仓价为基准). 每腿记一条 modify."""
    pairs = pairs_live()
    if not pairs:
        raise SystemExit("没有在场的对")
    pid, legs = next(iter(pairs.items()))
    failed = []
    for leg, tk in legs.items():
        try:
            r = call(leg, "POST", "/modify", json={"ticket": tk, "magic": MAGIC, "usd": usd})
            record({"pair_id": pid, "action": "modify", "leg": leg, "ticket": tk, "symbol": r["symbol"],
                    "side": r["side"], "volume": r["volume"], "open_price": r["price_open"], "open_ts": r.get("open_ts"),
                    "price": r.get("price_current"), "sl": r["sl"], "tp": r["tp"],
                    "sl_prev": r.get("sl_prev"), "tp_prev": r.get("tp_prev"),
                    "sl_usd": r.get("sl_usd"), "tp_usd": r.get("tp_usd"), "contract_size": r.get("contract_size"),
                    "retcode": r["retcode"], "ok": 1})
            bad = any(r.get(k) is None or abs(r[k] - usd) > usd * 0.05 for k in ("sl_usd", "tp_usd"))
            print(f"{pid} {leg} ticket {tk}: SL {r['sl']}(${r.get('sl_usd')}) TP {r['tp']}(${r.get('tp_usd')})"
                  + ("  <-- 实际值偏离要求" if bad else ""))
        except RuntimeError as e:
            failed.append(leg)
            record({"pair_id": pid, "action": "modify", "leg": leg, "ticket": tk, "ok": 0, "note": str(e)[:200]})
            print(f"!! {pid} {leg} ticket {tk} 修改失败: {e}")
    if failed:
        raise SystemExit(f"!! 两腿不一致: {','.join(failed)} 腿没改成, 再点一次更新或平一对")


def close_row(pid: str, leg: str, tk: int, c: dict, reason: str, note: str = "") -> dict:
    """平仓行: 券商侧实际数 + 开仓上下文, 全从 /close 或 /realized 的返回取."""
    return {"pair_id": pid, "action": "close", "leg": leg, "ticket": tk,
            "symbol": c.get("symbol"), "side": c.get("side"), "volume": c.get("volume"),
            "open_price": c.get("price_open"), "open_ts": c.get("open_ts"),
            "price": c.get("price_close") or c.get("price"), "sl": c.get("sl"), "tp": c.get("tp"),
            "sl_usd": c.get("sl_usd"), "tp_usd": c.get("tp_usd"), "contract_size": c.get("contract_size"),
            "reason": reason or "", "retcode": c.get("retcode"), "ok": 1,
            "profit": c.get("profit"), "swap": c.get("swap"), "commission": c.get("commission"),
            "total": c.get("total"), "balance_after": c.get("balance_after"), "note": note or c.get("result", "")}


def cmd_close(target: str):
    pairs = pairs_live()
    todo = pairs if target == "all" else {target: pairs.get(target)}
    if not todo or any(v is None for v in todo.values()):
        raise SystemExit(f"没有活跃的 pair {target}" if target != "all" else "没有活跃的对")
    for pid, legs in todo.items():
        results, failed = {}, False
        for leg, tk in legs.items():
            try:
                c = call(leg, "POST", "/close", json={"ticket": tk, "magic": MAGIC})
                if c["result"] == "ABSENT":   # 已被 SL/TP 或手动平: 结果仍从券商历史拿
                    c.update(call(leg, "GET", f"/realized/{tk}"))
                record(close_row(pid, leg, tk, c, "manual" if c["result"] == "CLOSED" else c.get("reason")))
                results[leg] = c
                print(f"{pid} {leg} ticket {tk}: {c['result']} @ {c.get('price_close') or c.get('price', '')}  "
                      f"结果 {c.get('total')}")
            except RuntimeError as e:
                failed = True
                record({"pair_id": pid, "action": "close", "leg": leg, "ticket": tk, "ok": 0, "note": str(e)[:200]})
                print(f"!! {pid} {leg} ticket {tk} 平仓失败: {e}")
        if not failed:
            settle(pid, results)


def main(argv):
    if not argv:
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    if cmd == "health":
        cmd_health()
    elif cmd == "status":
        cmd_status()
    elif cmd == "open":
        if not args:
            raise SystemExit("open SYMBOL [VOL] [--usd 500] [--a buy|sell]")
        symbol = args[0]
        vol = float(CFG.get("default_volume", 0.01))
        sl = tp = float(CFG.get("default_usd", 500))
        a_side = "buy"
        rest = args[1:]
        if rest and not rest[0].startswith("--"):
            vol, rest = float(rest[0]), rest[1:]
        for i in range(0, len(rest), 2):
            k, v = rest[i], rest[i + 1]
            if k == "--usd":
                sl = tp = float(v)
            elif k == "--a":
                a_side = v.lower()
            else:
                raise SystemExit(f"未知参数 {k}")
        if a_side not in ("buy", "sell"):
            raise SystemExit("--a 只能 buy|sell")
        cmd_open(symbol, vol, sl, tp, a_side)
    elif cmd == "close":
        cmd_close(args[0] if args else "all")
    elif cmd == "modify":
        if not args:
            raise SystemExit("modify USD")
        cmd_modify(float(args[0]))
    else:
        raise SystemExit(f"未知命令 {cmd}\n{__doc__}")


if __name__ == "__main__":
    main(sys.argv[1:])
