# mt5-hedge

一台 Windows、两个 MT5（两家券商、两个账户）、一对对冲单。页面开仓、改止损止盈、平仓；一腿先出场另一腿自动补平；每个动作记日志到本地 CSV 并发到你自己的 AWS。

## Windows 怎么用

**1. 安装**（管理员 PowerShell，一行，重跑 = 升级）

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; iex (irm https://raw.githubusercontent.com/cloudformer/mt5-hedge/main/installers/install.ps1)
```

装完桌面有三个图标：`MT5-A`、`MT5-B`、`mt5-hedge`。日志地址、局域网开关都由这个脚本统一写好，机器上不用填任何东西。

**2. 登录**：双击 `MT5-A`，登录券商 1 的账户，勾「记住密码」，关掉。`MT5-B` 同样登录券商 2。

**3. 启动**：双击 `mt5-hedge`。浏览器打开 `http://127.0.0.1:8000/`，顶部绿条「两腿就绪」就能用；红条写着哪腿为什么不能交易。

**4. 用**：
- 先点「pre-check」：三行结果，两腿就绪、本机时间与服务器差不超 5 分钟、这台机的公网 IP 没跑过。红的看原因。只检查不拦。
- 开一对：品种、手数、止损=止盈美元数（一个值）、方向（A 买涨·B 买跌 或反过来），确认。
- 平一对：随时点；「更新止损止盈」两腿一起改。
- 最下面「日志」：每对一个折叠条，在场的展开，已结的折叠，净值、平仓原因、两腿账号都在一行。

以后每次只做第 3 步。关掉黑窗口 = 全停，持仓不受影响（券商侧止损止盈还在）。

**局域网访问**：`http://<这台机的IP>:8000/`，Windows 防火墙放行 8000 即可。关掉局域网访问改安装脚本顶部的 `APP_HOST` 为 127.0.0.1。

## 文件在哪

| 东西 | 位置 |
|---|---|
| 两个 MT5 | `C:\Program Files\MT5-A`、`MT5-B`（便携模式，登录各自保存） |
| 配置 hedge.json、日志 CSV、运行日志 | `%USERPROFILE%\mt5-hedge\` |
| 代码 | PyPI 包 `mt5-hedge`（https://pypi.org/project/mt5-hedge/） |

## AWS（一次）

```
cd terraform
cp example.tfvars prod.tfvars     # 填 AK/SK、桶名、库密码
terraform init && terraform apply -var-file=prod.tfvars
```

输出 `log_url`，填进 `installers/install.ps1` 顶部的 `LOG_URL`，push 后所有机器装的时候自动带上。建的东西：一个私有 S3 桶、数据库 Lambda（s3rds）、收日志 Lambda，表自动建好。

**全网统一的三个值**只在 `installers/install.ps1` 顶部：包版本（锁定）、`LOG_URL`、`APP_HOST`。改这里 = 改全网，机器重跑那一行安装即生效。

**请求签名**：包里自带一把钥匙（`mt5_hedge/sign_key.txt`），发日志和 pre-check 都用它签名，接口只认签过的请求，用户不配置。换钥匙：生成新文件 → 发新版包 → 把旧钥匙填进 tfvars 的 `sign_key_prev` 后 apply → 全部机器升级完 → 清空再 apply，全程老包新包都能写。

## 日志表 hedge_log

一腿一动作一行，一对走完 4 行：open A、open B、close A、close B（中途改过止损止盈再加 modify 行）。每行自带全部上下文：时间、pair_id、腿、方向、手数、ticket、开仓价、止损止盈、平仓原因（manual / auto_naked / sl / tp）、券商侧 profit / swap / commission / total、平完余额、券商、账号、机器名、IP、进程号。主键 `key` 幂等，重发覆盖。表结构在 `terraform/schema/hedge_log.sql`。

## 纪律

- 每腿必带券商侧止损止盈，美元按该券商合约规格现算。
- 一腿开成功另一腿失败，自动平掉已开的腿。
- 一对只剩一腿，3 秒内市价补平。
- 两家合约规格不同，页面红条拦住，同手数不是对冲。
- 一次只允许一对在场。
