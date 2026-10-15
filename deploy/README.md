# 部署说明 · a-stock.chrisite.eu.cc

线上地址：<https://a-stock.chrisite.eu.cc> · 服务器 `192.3.13.21`（RackNerd，Ubuntu 24.04）

## 架构

```
浏览器 ──HTTPS──> Cloudflare ──> nginx :443 ──> 127.0.0.1:8848（uvicorn/FastAPI）
                                                    ↑
                                          scheduler.py 常驻采集（热搜 2min / 快讯 1min）
                                                    ↑
                                AI 层 ai.py ──> http://127.0.0.1:3065/v1（agent2api 内网直连）
```

应用只监听 `127.0.0.1:8848`，不直接对外暴露；对外仅经 nginx 443。
AI 分析层走服务器内网 `127.0.0.1:3065` 直连 agent2api 网关（248 个模型），绕开 Cloudflare WAF。

## AI 智能分析层

部署后站点新增 `/insight`（AI 洞察）页面与 6 个 AI 接口：

| 能力 | 接口 | 说明 |
|---|---|---|
| 快讯情绪解读 | 调度器自动 / `POST /api/ai/trigger` | 利好/利空/中性 + 影响度 + 题材 + 一句话解读，缓存进 `ai_reports` |
| 情绪统计 | `GET /api/sentiment` | 24h 情绪分布 / 多空指数 / 分时 |
| 每日复盘 | `GET /api/daily-review` | 盘后 15:30 后自动生成 Markdown 复盘 |
| 个股分析 | `GET /api/ai/stock/{code}` | K线技术面 + 消息面，缓存 24h |
| 任务进度 | `GET /api/ai/task` | 后台 AI 任务状态轮询 |
| 实时流 | `GET /api/stream` | SSE 新快讯 + AI 解读推送 |

**密钥不在版本库**，经 `/opt/daily-brief/data/ai.env`（权限 600）注入 systemd：

```bash
# /opt/daily-brief/data/ai.env
AI_API_KEY=sk-a2a-xxx
```

systemd 用 `EnvironmentFile=-/opt/daily-brief/data/ai.env` 读取；首次部署 `install.sh` 会生成空模板，
需手工填入 `AI_API_KEY` 后 `systemctl restart daily-brief-web daily-brief-scheduler`。

> **Cloudflare 100s 网关超时**：批量 AI 触发（20 条解读 + 复盘）耗时 3–5 分钟，远超 CF 网关 100s，
> 因此 `/api/ai/trigger` 与复盘重算都改成**后台线程 + 轮询**，接口立即返回。

## 一键部署

```bash
git clone --depth=1 https://github.com/SuCriss/daily-brief.git /opt/daily-brief
bash /opt/daily-brief/deploy/install.sh
```

脚本幂等，可重复执行用于更新。它依次完成：

1. clone / `git fetch && git reset --hard origin/main`
2. `mkdir -p /opt/daily-brief/data` + 生成/锁定 `ai.env`
3. 建 venv 并 `pip install -r requirements.txt`
4. 初始化 SQLite（`DAILY_BRIEF_DB=/opt/daily-brief/data/brief.db`）并加载股票/板块字典
5. 安装 `daily-brief-web.service` + `daily-brief-scheduler.service` 并 enable --now
6. 写入 `/etc/nginx/conf.d/a-stock.chrisite.eu.cc.conf`（含 `/api/stream` SSE 关缓冲）并 reload

## 服务器路径

| 路径 | 说明 |
|---|---|
| `/opt/daily-brief/` | 源码（git clone） |
| `/opt/daily-brief/data/brief.db` | SQLite 数据库（不在 git 里） |
| `/opt/daily-brief/venv/` | Python 虚拟环境 |
| `/etc/systemd/system/daily-brief-web.service` | Web 服务 |
| `/etc/systemd/system/daily-brief-scheduler.service` | 采集调度 |
| `/etc/nginx/conf.d/a-stock.chrisite.eu.cc.conf` | 域名反代 |
| `/etc/letsencrypt/live/a-stock.chrisite.eu.cc/` | TLS 证书 |

## 运维

```bash
systemctl status daily-brief-web daily-brief-scheduler
journalctl -u daily-brief-scheduler -f              # 采集节奏
journalctl -u daily-brief-web -f                    # 访问日志
curl -s http://127.0.0.1:8848/healthz
cd /opt/daily-brief && bash deploy/install.sh       # 更新代码
```

## 迁移历史数据

```bash
# 本地导出（用 sqlite3 的 .backup，避免 WAL 下拷出不一致快照）
python -c "import sqlite3; s=sqlite3.connect('data/brief.db'); d=sqlite3.connect('seed.db'); s.backup(d)"
scp seed.db root@192.3.13.21:/opt/daily-brief/data/brief.db
ssh root@192.3.13.21 'systemctl restart daily-brief-web daily-brief-scheduler'
```

## 两个必须知道的坑

### 1. 证书续期必须用 webroot，不能用 standalone

本域名最初的 certbot renewal 配置是 `authenticator = standalone`，它需要独占 80 端口。
nginx 常驻后 80 端口被占用，续期必然失败 —— 结果是证书在 **2026-08-12 到期后一直没续上**，
源站 TLS 早已过期，只是因为流量经 Cloudflare（边缘有自己的有效证书）才没暴露出来。

排查命令：

```bash
openssl x509 -noout -dates -in /etc/letsencrypt/live/a-stock.chrisite.eu.cc/fullchain.pem
echo | openssl s_client -connect 127.0.0.1:443 -servername a-stock.chrisite.eu.cc 2>&1 | grep 'Verify return code'
```

正确配置（`deploy/install.sh` 不负责改这个，需手工确认）：

```bash
# /etc/letsencrypt/renewal/a-stock.chrisite.eu.cc.conf
[renewalparams]
authenticator = webroot
webroot_path = /var/www/html,

[[webroot_map]]
a-stock.chrisite.eu.cc = /var/www/html
```

对应 nginx 站点里保留：

```nginx
location ^~ /.well-known/acme-challenge/ {
    root /var/www/html;
    default_type text/plain;
}
```

验证：

```bash
certbot renew --dry-run --cert-name a-stock.chrisite.eu.cc
certbot renew --cert-name a-stock.chrisite.eu.cc --force-renewal
nginx -t && systemctl reload nginx
```

> 同一台机器上 `hermes.chrisite.eu.cc` 也仍是 `standalone`，`agent2api` / `cpa` / `openclaw` / `sub2api`
> 用的是 `nginx` 插件，`wb2api` 用 `webroot`。除 hermes 外都不受此问题影响。

### 2. `requirements.txt` 必须包含 `python-multipart`

`app.py` 里有 `Form(...)` 端点（`/login`、`/register`、`/monitor-keywords`），
FastAPI 在**导入时**就会检查该依赖，缺失会直接 `RuntimeError`，表现为服务起不来。

## 与旧项目 easy-stock 的关系

同一域名原先是 `easy-stock`（A股 AI 智能投研工作台，Go 后端 + React 前端，`/opt/easy-stock`）。
2026-10-15 已卸载，卸载脚本与备份见 `_deploy/uninstall-easy-stock.sh`：

- 备份目录 `/root/uninstall-easy-stock-backup/`（旧 nginx 配置、Basic Auth 凭据、systemd 单元、`settings.json`）
- 保留了 Let's Encrypt 证书目录（域名未变，新项目直接复用）
