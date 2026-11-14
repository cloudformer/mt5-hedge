-- mt5-hedge 日志表: 一腿一动作一行, 每行自带全部上下文. terraform apply 自动执行(幂等).
-- 一对走完 = 4 行: open A / open B / close A / close B; modify 可选. 分析只需 WHERE action='close' 按 pair_id 分组.
CREATE TABLE IF NOT EXISTS hedge_log (
  key           TEXT PRIMARY KEY,   -- 幂等键 pair_id_ticket_action(modify 加时间), 重发覆盖
  ts_utc        TEXT,               -- 动作时间 ISO8601 UTC
  pair_id       TEXT,               -- 对冲对编号(唯一分组键)
  action        TEXT,               -- open / modify / close
  leg           TEXT,               -- A / B(以后可加 C)
  side          TEXT,               -- buy / sell
  symbol        TEXT,
  volume        REAL,
  ticket        INTEGER,            -- MT5 持仓号
  open_price    REAL,
  open_ts       TEXT,
  price         REAL,               -- 本动作成交价/当时价
  sl            REAL,
  tp            REAL,
  sl_usd        REAL,
  tp_usd        REAL,
  sl_prev       REAL,               -- modify: 改前
  tp_prev       REAL,
  reason        TEXT,               -- close: manual / auto_naked / rollback / sl / tp / stopout
  retcode       INTEGER,
  ok            INTEGER,            -- 1 成功 0 失败
  profit        REAL,               -- close: 券商侧
  swap          REAL,
  commission    REAL,
  total         REAL,
  balance_after REAL,
  broker        TEXT,               -- 页面名 券商1/券商2
  company       TEXT,               -- MT5 读出
  server        TEXT,
  login         INTEGER,
  account       TEXT,
  currency      TEXT,
  contract_size REAL,
  host_name     TEXT,               -- 本机(两腿应相同)
  host_lan_ip   TEXT,
  host_pub_ip   TEXT,
  app_pid       INTEGER,
  app_started   TEXT,
  note          TEXT,
  received_at   TEXT                -- 接口收到的时间(接口填)
);
CREATE INDEX IF NOT EXISTS hedge_log_pair ON hedge_log (pair_id, ts_utc);
CREATE INDEX IF NOT EXISTS hedge_log_ts   ON hedge_log (ts_utc);
