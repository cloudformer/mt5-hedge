# mt5-hedge

One Windows, two MT5 terminals, one hedged pair: a local page to open, modify and close the pair, a watchdog that
closes a naked leg, and a log of every action sent to your own AWS.

- `mt5_hedge` — the Windows side (`mt5-hedge` command, the page on port 8000).
- `mt5_hedge.server` — the log endpoint Lambda and the `hedge_log` table, deployed by the repository's `terraform/`.

Install and use: https://github.com/cloudformer/mt5-hedge
