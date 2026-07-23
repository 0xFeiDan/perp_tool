# 多交易所 BBO 策略控制台

这是一个用于 **Lighter、Hyperliquid 与 Binance USD-M Futures 正式网** 的单策略控制台。页面按 Lighter 风格展示行情、下单与跟价状态；后端持有交易所密钥，浏览器永远不直接接触交易所私钥。

MT5 在本项目中是 **只读数据源**，不是交易场所：它不能开仓、平仓、改单、撤单，也不会出现在可执行交易所列表中。

> 风险提示：这不是仓位或损失限额系统。输入金额会换算为合约基础数量，但交易所自身的最小下单额、精度、保证金、风险限额和仓位限额仍然生效。先以 `TRADING_ENABLED=false` 运行和核对，再决定是否人工打开真实交易。

## 当前已实现的交易规则

- 市价模式：买单以当前**卖一**作为 `Limit + IOC` 价格，卖单以当前**买一**作为 `Limit + IOC` 价格；未在该价位成交的剩余部分立即取消。
- 跟价挂单：买单跟随买一、卖单跟随卖一，以 `Post Only` 挂单；Lighter / Hyperliquid 使用交易所改单能力，Binance 对本策略确认过的订单执行“撤指定单后按新价重挂”。
- 开仓：买入为开多，卖出为开空；平仓始终带 `reduce_only`。
- 金额输入：按 USDC（Lighter、Hyperliquid）或 USDT（Binance）名义金额输入，后端根据实时 BBO 换算基础币数量并按交易所步长向下取整。
- 单一活跃策略：同一时间后端只允许一个“交易所 + 合约”处于活跃状态，防止页面选择与后端真实执行市场不一致。

## 下单安全链路：不能跳过预览或确认

浏览器不能提交任意交易所 symbol / market ID 来下单。完整流程如下：

1. 后端从交易所加载市场元数据，为每个“交易所 + 外部市场 ID”生成稳定、仅供控制面引用的 `internal_instrument_id`；它不是安全凭据。
2. 页面只保存和提交该内部 ID；后端解析后才会选择真实外部市场。即使两个交易所都叫 `BTC`，它们的内部 ID 也不同，不能跨交易所复用。
3. 页面向 `POST /api/order-intents` 请求预览。后端重新读取当前 BBO、核验行情新鲜度、计算数量，并返回显示用的合约、方向、开/平、参考价、预估数量、`reduce_only`、`Post Only / IOC` 等字段。
4. 浏览器只弹出**一次**确认框。确认后才向 `POST /api/execute` 提交一次性意图令牌、幂等请求 ID 和 `confirm_live=true`。
5. 后端把一次性令牌绑定到当前会话，令牌默认 30 秒失效；执行前再次校验活跃合约、交易所、最新 BBO、价格与数量是否与预览一致。行情变化、合约切换、令牌过期或重复请求都会拒绝执行，必须重新预览。

这意味着：金额、方向、模式、开平、市场和交易所都由已确认的服务器端意图决定，而不是由最后一个浏览器请求决定。

## 两层真实交易开关

真实订单必须同时满足下面所有条件：

1. 全局 `TRADING_ENABLED=true`；
2. 对应交易所的 `LIGHTER_LIVE_TRADING=true`、`HYPERLIQUID_LIVE_TRADING=true` 或 `BINANCE_LIVE_TRADING=true`；
3. 对应交易所的凭据完整且可用；
4. 控制台已使用 `CONTROL_PLANE_TOKEN` 登录，CSRF 校验通过；
5. 用户在页面勾选真实交易并完成这次预览对应的确认；
6. BBO 仍在 `MAX_QUOTE_AGE_MS` 允许的新鲜度内，且预览条件没有变化。
7. 当 `TRADING_ENABLED=true` 时，PostgreSQL 的一次性意图、幂等键和“交易所调用前订单记录”均可用；`DURABLE_EXECUTION_REQUIRED=true` 时数据库不可用会直接拒绝启动真实执行。

任一条件不满足时，后端拒绝向交易所提交订单。默认配置为：

```dotenv
TRADING_ENABLED=false
DURABLE_EXECUTION_REQUIRED=true
LIGHTER_LIVE_TRADING=false
HYPERLIQUID_LIVE_TRADING=false
BINANCE_LIVE_TRADING=false
```

不要为了绕过交易所的 `MAX_POSITION_BASE`、最小名义金额或风险限制去修改后端；这些限制属于交易所风险控制，仍应保留。

## 认证与当前生产边界

当前运行时是**单一操作员**模型：一个至少 32 字符的 `CONTROL_PLANE_TOKEN` 换取短时 `HttpOnly + SameSite=Strict` 会话 Cookie，并要求写操作携带 CSRF 令牌。它适合受控的个人/小团队私有控制面，不是多用户权限系统。

- 当前未接入“用户表 + Argon2 密码 + TOTP + 角色权限 + 用户级审计”的完整登录流程。
- 即使仓储中已有 PostgreSQL、Redis、迁移和领域模型基础设施，也**不能**据此宣称多人生产权限体系已经完成。
- 在接入并审计上述身份系统之前，不要把此界面作为公开 SaaS、多人共享交易终端或对外暴露的管理后台。

控制面还包含精确 Host/Origin 限制、控制令牌登录限速、写请求 CSRF 校验、会话/执行限速、请求幂等、WebSocket 同源会话校验、`no-store` 安全响应头和不含密钥的审计记录。审计文件位于运行时目录，只记录必要的操作元数据，不应包含私钥、签名、Cookie 或控制令牌。

## 本地开发（仅回环地址）

1. 从模板创建本地私密配置，且不要提交它：

   ```powershell
   Copy-Item .\backend\.env.example .\backend\.env
   ```

2. 在 `backend/.env` 中生成并填写至少 32 字符的 `CONTROL_PLANE_TOKEN`。它是网页控制台口令，**不是**交易所 API Key。
3. 如需加载真实市场，可填写所需交易所的只读/交易凭据，但保持所有 `*_LIVE_TRADING=false` 和 `TRADING_ENABLED=false`。
4. 安装依赖并仅监听本机：

   ```powershell
   $python = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe'
   & $python -m pip install -r .\backend\requirements.txt
   & $python -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8790 --no-access-log
   ```

5. 在本机访问 `http://127.0.0.1:8790`，先输入控制台令牌，再选择交易所和合约。

不要将 Uvicorn 的 `8790` 端口映射到公网，也不要把真实 `backend/.env`、运行时目录或屏幕录制上传到 Git、网盘或工单。

## Ubuntu 私有部署：Docker Compose + Tailscale Serve

生产建议只允许通过 Tailnet 访问：Docker 不发布 8790、80 或 443 到公网。当前 `docker-compose.yml` 中 Caddy 仅绑定主机回环地址 `127.0.0.1:8080`，PostgreSQL、Redis 与后端仅在 Compose 私有网络中通信；Tailscale Serve 在主机上终止 HTTPS 并转发到该回环地址。

### 1. 准备配置和目录权限

在 Ubuntu 项目根目录执行：

```bash
cp backend/.env.example backend/.env
cp deploy/compose.env.example deploy/compose.env
chmod 600 backend/.env deploy/compose.env
```

- `backend/.env`：仅放控制台令牌、交易所凭据、精确 Host/Origin、全局与单交易所开关。
- `deploy/compose.env`：仅放 `POSTGRES_PASSWORD`、`REDIS_PASSWORD` 等 Docker 基础设施密码；不要放交易所 API Key。
- 两个数据库/Redis 密码都使用随机长字符串；容器启动入口会安全编码连接 URL，因此不需要为了手工 URL 拼接而降低密码复杂度。
- 首次上线保持 `TRADING_ENABLED=false` 和所有交易所 `*_LIVE_TRADING=false`。
- Compose 会从独立的 PostgreSQL 组件变量构造 `DATABASE_URL`。不要在 `backend/.env` 中手写带密码的 URL；只有在本地调试持久化时才显式提供自己的数据库 URL。

当访问地址为例如 `https://your-node.tailnet-name.ts.net` 时，至少修改 `backend/.env`：

```dotenv
PUBLIC_HTTPS=true
ALLOWED_HOSTS=your-node.tailnet-name.ts.net
ALLOWED_ORIGINS=https://your-node.tailnet-name.ts.net
TRUST_PROXY_HEADERS=false
BIND_SESSION_TO_IP=false
```

这里必须使用你的**准确** Tailnet HTTPS 主机名；不要使用 `*`、通配域或把公网 IP 写进 Origin。`BIND_SESSION_TO_IP=false` 是 Tailscale/反向代理场景的推荐值，避免移动网络或代理路径变化使会话失效；当前 Tailscale Serve + Caddy 链路也不需要信任代理传来的客户端 IP，故保持 `TRUST_PROXY_HEADERS=false`。

### 2. 先验证 Compose 配置，再启动

```bash
docker compose --env-file deploy/compose.env config >/dev/null
docker compose --env-file deploy/compose.env up -d --build
docker compose --env-file deploy/compose.env ps
curl --fail http://127.0.0.1:8080/healthz
```

Compose 会在启动阶段运行数据库迁移；后端根文件系统为只读，仅将受控运行时数据写入独立 volume。若 `config`、迁移或健康检查失败，不要打开任何真实交易开关，先通过 `docker compose --env-file deploy/compose.env logs --tail=200 <service>` 排查。

### 3. 使用 Tailscale Serve 暴露 HTTPS，而不是端口映射

确认 Caddy 本地健康后，在 Ubuntu 主机配置 Tailnet HTTPS 转发：

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8080
tailscale serve status
```

随后只通过 `https://<该主机的-tailnet-名称>.ts.net` 访问控制台。不要增加 `ports: "8790:8790"`、`"80:80"` 或 `"443:443"`，也不要将 MT5 sidecar 的 8900 暴露到网络。主机防火墙应继续拒绝来自公网的 8790、8080 和 8900。

### 4. API 密钥最小权限

- Binance：只创建 USD-M Futures 的 `TRADE` Key，绑定 Ubuntu 出口 IP，关闭提币、现货、划转、子账户管理及其他未使用权限。
- Hyperliquid / Lighter：为本策略使用独立 API Wallet / API Key 和独立资金隔离，不复用主钱包私钥。
- 控制台令牌、交易所密钥、数据库密码和 MT5 sidecar 令牌必须相互独立；轮换任一个密钥后，应重启相关服务并使旧会话失效。

## MT5：Ubuntu/Wine 只读 Sidecar

### 安全模型

MT5 终端不能作为 Docker 里的交易执行器。生产结构是：

```text
浏览器 -- Tailnet HTTPS --> Caddy(127.0.0.1:8080) --> Docker 后端
                                                       |
                                             只读 Unix Socket + 独立令牌
                                                       |
                                      Ubuntu/Wine MT5 sidecar (127.0.0.1:8900)
                                                       |
                                      MT5 终端 + Investor Password（只读）
```

- `backend/mt5_readonly.py` 采用固定的本地只读方法白名单；当前 HTTP sidecar 仅提供状态、账户、持仓和单合约报价读取。没有通用 RPC，也没有交易、下单、改单或撤单方法。
- sidecar 启动时同时检查终端和账户的 `trade_allowed == false`。任一检查失败即关闭并拒绝服务。
- 主后端只接受 loopback TCP 或 Unix socket 连接 MT5 sidecar；生产 overlay 使用 Unix socket，socket 之外还必须使用独立、至少 32 字符的 `MT5_SIDECAR_TOKEN`。
- MT5 的 Investor Password 仅留在 Ubuntu 主机 `/etc/lighter/mt5-readonly.env`，绝不能写入 Docker 核心的 `backend/.env`、Compose 文件、日志或浏览器。

### 部署顺序

前提：在 Ubuntu 主机的隔离用户 `mt5ro` 下，已经安装 Wine、MT5 终端和包含 `MetaTrader5`、FastAPI、Uvicorn 的 Wine Python。项目提供 sidecar 启动与 systemd 模板，但**不会**替你下载经纪商终端、创建 MT5 账户或验证 Investor Password。

1. 复制 sidecar 私密配置并仅授予 root/`mt5ro` 读取：

   ```bash
   sudo install -d -m 0750 /etc/lighter
   sudo cp deploy/mt5-readonly.env.example /etc/lighter/mt5-readonly.env
   sudo chmod 600 /etc/lighter/mt5-readonly.env
   ```

   填写 Investor Password、服务器名、Wine Python 路径和独立 `MT5_SIDECAR_TOKEN`。禁止使用主密码。

2. 安装并启动两个主机服务：

   ```bash
   sudo cp deploy/mt5-readonly-sidecar.service /etc/systemd/system/
   sudo cp deploy/mt5-readonly-socket-proxy.service /etc/systemd/system/
   sudo chmod 750 deploy/run-mt5-sidecar.sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now mt5-readonly-sidecar.service mt5-readonly-socket-proxy.service
   sudo systemctl status mt5-readonly-sidecar.service mt5-readonly-socket-proxy.service
   ```

   socket bridge 只在 `/run/lighter-mt5/mt5.sock` 创建本地 Unix socket，并以受限组权限交给容器；sidecar 的 TCP 监听仍是 `127.0.0.1:8900`。

3. 在 Docker 核心 `backend/.env` 中保持本地 MT5 直连关闭，并只配置 socket 与**同一个** sidecar token：

   ```dotenv
   MT5_READONLY_ENABLED=false
   MT5_SIDECAR_URL=
   MT5_SIDECAR_UNIX_SOCKET=/run/mt5-sidecar/mt5.sock
   MT5_SIDECAR_TOKEN=replace-with-the-separate-32-plus-character-token
   MT5_SIDECAR_TIMEOUT_SECONDS=5
   ```

4. 使用 MT5 overlay 启动。它只读挂载 socket 到后端容器：

   ```bash
   docker compose --env-file deploy/compose.env \
     -f docker-compose.yml -f deploy/docker-compose.mt5.yml up -d --build
   ```

5. 在不启用真实交易的状态下连续观察 MT5 账户、持仓和报价至少 7 × 24 小时，核对时间、合约名、报价、净持仓与 sidecar 重连行为。任何 `readonly_verified=false`、sidecar 不可用、终端可交易或账户可交易的状态，都应视为失败并保持只读/停用。

MT5 API 路由仅用于受认证控制台的只读展示。即使 MT5 sidecar 工作正常，它也不会为 Lighter、Hyperliquid 或 Binance 创建、修改或关闭订单。

## 上线前检查清单

- [ ] `backend/.env`、`deploy/compose.env`、`/etc/lighter/mt5-readonly.env` 均不在 Git 中，权限为 `0600`。
- [ ] `docker compose ... config`、数据库迁移和 `/healthz` 均成功。
- [ ] 只存在 `127.0.0.1:8080` 的 Docker 发布端口；公网没有 8790、8080、8900。
- [ ] Tailscale Serve 指向 `http://127.0.0.1:8080`，浏览器地址与 `ALLOWED_HOSTS` / `ALLOWED_ORIGINS` 完全一致。
- [ ] 全局和各交易所真实交易开关仍为 `false`，可加载行情且下单被后端拒绝。
- [ ] 对每个交易所确认 UI 所选合约、`internal_instrument_id`、预览合约和交易所返回订单属于同一市场。
- [ ] 仅在人工复核后，用最小可接受金额做一次真实交易测试；自动化测试不得调用真实 `/api/execute`。
- [ ] 启用跟价前已理解：服务重启不会自动恢复旧跟价单；重启/迁移前应先在页面取消跟价，并在各交易所核对订单。
- [ ] MT5 使用 Investor Password，并已通过连续只读观察；没有把 MT5 主密码交给服务、容器或浏览器。

## 常见拒绝原因

| 提示 | 含义与处理 |
| --- | --- |
| `TRADING_ENABLED=false` | 全局杀开关仍关闭；先完成只读验证后，再由操作员人工修改并重启。 |
| `quantity exceeds MAX_POSITION_BASE` | 交易所仓位/风控上限拒绝，不是前端可安全绕过的限制。检查现有仓位、保证金、风险参数或降低名义金额。 |
| 行情过期 / 预览条件变化 | BBO 已变或超过 `MAX_QUOTE_AGE_MS`；重新预览并重新确认。 |
| instrument / exchange mismatch | 页面市场列表过期、跨交易所复用了内部 ID，或另一会话切换了活跃合约；刷新市场并重新选择。 |
| MT5 sidecar 不可用 / `readonly_verified=false` | 保持 MT5 停用，检查 Investor 登录、Wine 终端、Unix socket、独立 token 和 systemd 日志；不要改成主密码或开放网络端口。 |

## 尚未完成、不能误认为已具备的能力

- 多用户账户、Argon2 密码、TOTP、角色权限和用户级会话/审计尚未接入当前运行时。
- 未提供公开互联网部署模式；推荐且文档化的入口是 Tailscale Serve 私有 HTTPS。
- MT5 仅做 Ubuntu/Wine 只读数据桥接，尚未也不会被设计成交易执行通道。
- PostgreSQL 已用于持久化一次性下单意图（仅令牌哈希）、幂等键、交易所调用前订单和后续状态审计；Redis 目前只作为部署预留，尚未用于多实例会话共享。
- “持久化后恢复活跃跟价单/自动继续交易”没有作为安全特性启用；重启后的未执行确认会自然失效，重启前应先取消并核对所有跟价单。
