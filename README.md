# 每日快讯系统（逆向复刻版）

对 `sanhubaotuan.com`（vip001.net 信息监控台）"每日快讯"底层逻辑的逆向分析与等价自建实现。
**不依赖该站任何账号**：它展示的数据本身来自公开上游源，本项目直接对接同样的上游源。

---

## 一、逆向结论：目标站的底层逻辑

目标站是三层结构，付费墙只在最上层。

### 第 1 层 · 采集层（公开上游源）

目标站自己并不生产数据，它是**聚合器**。从页面文案和数据结构可确认它采集：

| 类别 | 上游源 | 观察到的同步节奏 |
|---|---|---|
| 热搜 | 微博热搜、百度热搜、抖音热搜 | 页面自述"每 2 分钟同步一次" |
| 快讯 | 财联社电报、东方财富 7×24、新浪财经滚动 | 分钟级轮询 |

页面自述原文（`/news-wire`）：`每 2 分钟同步一次微博、百度、抖音热搜。列表按抓取时间倒序排列`。

### 第 2 层 · 抽取层（名称匹配 + 统计）

页面 `/stocks`、`/sectors`、`/theme-hotspots` 的自述：
> 统计异动信息提及个股次数，做**热度排名**、**趋势变化**。

即：对每条快讯正文做**股票名称/代码匹配**与**板块词匹配**，把结果落成"条目 → 标的"的提及关系，再按时间窗口聚合出排名与趋势。这是个纯文本抽取 + 计数聚合的逻辑，没有黑盒。

### 第 3 层 · 展示与分层层（付费墙在这里）

| 页面 | 访客策略（实测） |
|---|---|
| `/`、`/stocks`、`/sectors`、`/theme-hotspots`、`/x-feed`、`/board-fund-flow`、`/monitor-keywords` | **会员专属**，返回 `member-only-page` 门禁页 |
| `/search` | 开放，但限"**当前只能查看 3 小时前信息**" |
| `/news-wire`、`/commodities`、`/daily-review` | 完全开放 |

关键结论：**数据采集和统计逻辑与会员无关，会员只控制"什么时候给你看"**。所以自建一套同等能力完全不需要它的账号。

### 逆向出的关键算法：财联社接口签名

财联社 `/v1/roll/get_roll_list` 需要签名，实测算法为：

```
sign = md5( sha1( "&".join(f"{k}={v}" for k,v in sorted(params.items())) ) )
```

参数按 key 字典序拼接 → SHA1 取 hex → 再 MD5 取 hex。这是目标站能持续拉到电报数据的核心，本项目 `sources.py::cls_sign` 原样复现。

---

## 二、本项目结构

```
daily-brief/
├── db.py           数据层（SQLite）：raw_items / brief_items / stocks / sectors / mentions / members …
├── sources.py      采集层：6 个上游源适配器 + 财联社签名 + 去重落库
├── imgs.py         图片归一化层：统一各家图片格式、过滤占位图、代理白名单
├── kline.py        K线数据层：日/周/月 + 分钟级 K 线，归一化、均线计算与缓存
├── extract.py      抽取层：A股字典(5573只) + 板块词典 + 提及抽取 + 热度排名/趋势
├── app.py          Web 层：FastAPI 路由 + 会员分层 + 认证 + 图片代理 + K线接口
├── scheduler.py    调度层：热搜 2min / 快讯 1min 周期采集
├── cli.py          命令行工具
├── verify.py       端到端验证脚本
└── web/*.html      Jinja2 模板（kline.js 为纯 Canvas K线弹窗，无第三方依赖）
```

### 股票 K 线弹窗

在每日快讯和股票统计页，**点击任意个股标签/名称**即可弹出 K 线详情。

弹窗内容：

- **周期切换**：日K / 周K / 月K / 60分 / 15分 / 5分
- **均线**：MA5 / MA10 / MA20 / MA60，图例可点击开关
- **主图**：蜡烛图（红涨绿跌、阳线空心）+ 均线叠加
- **副图**：成交量柱
- **十字光标**：鼠标/触摸移动显示该根 K 线的开高低收、涨跌幅、成交量
- **统计栏**：最新价、涨跌额与涨跌幅、今开、最高、最低、成交量、周期、数据源
- **关联快讯**：该股近 72 小时在快讯中被提及的记录

数据源（均为实测可用的公开行情接口）：

| 周期 | 接口 | 说明 |
|---|---|---|
| 日/周/月 | `web.ifzq.gtimg.cn/appstock/app/fqkline/get` | 腾讯，前复权，支持沪深创科与指数 |
| 5/15/30/60 分 | `money.finance.sina.com.cn/.../CN_MarketData.getKLineData` | 新浪 |
| 分时 | `web.ifzq.gtimg.cn/appstock/app/minute/query` | 腾讯 |

> 注意腾讯 K 线数组顺序是 `[日期, 开, 收, 高, 低, 量]` —— **收盘在第 3 位而非第 2 位**，这是该接口最容易踩的坑（kline.py 已按此解析）。

代码→腾讯符号映射优先用 `stocks.market` 字段，缺失时按代码首位推断（6→sh、0/3→sz、4/8/9→bj）。

接口：`GET /api/kline?code=600519&period=day&count=180`（需登录；非会员限 60 根）、`GET /api/minute?code=600519`、`GET /api/indexes`。内存缓存：日线 5 分钟、分钟线 60 秒，命中后从 ~1.9s 降到 ~8ms。

### 图片处理（imgs.py）

各上游图片格式不统一，实测有两种形态：

| 来源 | 原始格式 | 处理 |
|---|---|---|
| 财联社 | `["https://image.cls.cn/xxx.png"]` | 字符串列表，直接可用 |
| 新浪 | `[{"u":"https://n.sinaimg.cn/xxx.jpg","w":400,"h":300,"t":""}]` | 字典 → 取 `u` 字段 |

`imgs.normalize_images()` 统一完成：

1. **字典 → URL**：取 `u`/`url`/`img`/`src`/`cover` 字段
2. **过滤占位图**：新浪 `wx_poster_finance1.png`（100×100）、财联社 `share/roll.png` 与 `share/vip.png`，以及任何宽高 ≤120 的图
3. **http → https 升级**：`n.sinaimg.cn`、`image.cls.cn`、`img.cls.cn` 实测支持 https，避免混合内容拦截
4. **去重 + 限量**（单条最多 6 张）

图片在入库前（`persist_brief`）与出库时（`query_brief`）各归一化一次，后者兼容历史数据。

另提供 `/img?u=...` 图片代理接口，带图床白名单校验（防 SSRF，拒绝 `n.sinaimg.cn.evil.com` 这类伪装域名），用于防盗链或混合内容兜底。实测两个图床均无防盗链，可直连。

> 图片覆盖率取决于上游本身：新浪约 27% 的财经新闻带图，财联社约 15%，东财 7×24 与热搜类本身不提供配图。这是上游数据特性，不是渲染缺陷。

数据模型与目标站同构：

```
raw_items   原始上游条目
  └─ brief_items   每日快讯条目（时间线单元）
       └─ mentions 条目→个股/板块 提及关系（统计层原料）
stocks      5573 只 A 股字典（名称/代码）
sectors     136 个板块/题材词典
members/sessions/marks/alerts   账号体系
```

---

## 三、快速开始

```bash
pip install fastapi uvicorn requests jinja2 python-multipart

# 1) 初始化 + 加载股票字典 + 跑一轮完整采集与抽取
python cli.py pipeline

# 2) 创建会员账号（第一个注册的账号自动是 admin）
python cli.py adduser myuser --role vip --days 365

# 3) 启动 Web（默认 http://127.0.0.1:8848）
python cli.py serve --port 8848

# 4) 后台常驻采集调度
python scheduler.py

# 5) 端到端验证
python verify.py
```

常用命令：

```bash
python cli.py stats                      # 查看数据统计
python cli.py ranking stock  --hours 24  # 个股热度排名
python cli.py ranking sector --hours 24  # 板块热度排名
python cli.py fetch                      # 只跑采集
python cli.py extract                    # 只跑抽取
```

---

## 四、页面与分层

| 路由 | 说明 | 访客 |
|---|---|---|
| `/` | 每日快讯时间线（主页面） | 门禁页 |
| `/stocks` | 股票统计：热度排名 + 个股异动明细 | 门禁页 |
| `/sectors` | 板块统计：热度排名 + 板块异动明细 | 门禁页 |
| `/monitor-keywords` | 自定义关键词监控 | 需登录 |
| `/news-wire` | 全网热搜 | 开放 |
| `/search` | 全站搜索（快讯/热搜/个股/板块） | 开放，限 3 小时前 |
| `/api/kline` | K线数据（日/周/月/分钟，含均线与关联快讯） | 需登录 |
| `/api/minute` | 当日分时 | 需登录 |
| `/api/indexes` | 上证/深证/创业板指数快照 | 开放 |
| `/api/brief` `/api/stocks` `/api/sectors` `/api/hot` `/api/stats` | JSON 接口 | 跟随分层 |

分层开关：`GATE_ENABLED=0` 环境变量可让访客也直接看到全部页面（自建站点可自行决定）。

---

## 五、上游源与可用性（实测）

| 源 | 接口 | 状态 |
|---|---|---|
| 微博热搜 | `weibo.com/ajax/side/hotSearch` | ✅ 需 Referer |
| 百度热搜 | `top.baidu.com/api/board` | ✅ 注意 UTF-8 编码 |
| 抖音热搜 | `douyin.com/aweme/v1/web/hot/search/list/` | ✅ 需 Referer |
| 财联社电报 | `cls.cn/v1/roll/get_roll_list` | ✅ 需 md5(sha1()) 签名 |
| 东财 7×24 | `np-weblist.eastmoney.com/.../getFastNewsList` | ✅ |
| 新浪财经滚动 | `feed.mix.sina.com.cn/api/roll/get` | ✅ |
| A 股字典 | `vip.stock.finance.sina.com.cn/.../getHQNodeData` | ✅ 5573 只 |

采集层带重试、去重指纹（标题+正文归一化后 SHA1），单个源失败不影响其他源。

---

## 六、服务器部署（a-stock.chrisite.eu.cc）

线上地址：<https://a-stock.chrisite.eu.cc> · 服务器 `192.3.13.21`（Ubuntu 24.04）

```
浏览器 ──HTTPS──> Cloudflare ──> nginx :443 ──> 127.0.0.1:8848（uvicorn/FastAPI）
                                                    ↑
                                          scheduler.py 常驻采集
```

一键部署（在服务器上执行）：

```bash
git clone --depth=1 https://github.com/SuCriss/daily-brief.git /opt/daily-brief
bash /opt/daily-brief/deploy/install.sh
```

脚本做的事：拉代码 → 建 venv 装依赖 → 初始化 SQLite 与股票字典 → 安装
`daily-brief-web.service` + `daily-brief-scheduler.service` → 写入 nginx 站点配置并 reload。
可重复执行，用于后续更新。

| 服务器路径 | 说明 |
|---|---|
| `/opt/daily-brief/` | 源码（git clone） |
| `/opt/daily-brief/data/brief.db` | SQLite 数据库（**不在 git 里**，随部署生成） |
| `/opt/daily-brief/venv/` | Python 虚拟环境 |
| `/etc/systemd/system/daily-brief-web.service` | Web 服务（127.0.0.1:8848） |
| `/etc/systemd/system/daily-brief-scheduler.service` | 采集调度（热搜 2min / 快讯 1min） |
| `/etc/nginx/conf.d/a-stock.chrisite.eu.cc.conf` | 域名反代配置 |

常用运维：

```bash
systemctl status daily-brief-web daily-brief-scheduler
journalctl -u daily-brief-scheduler -f          # 看采集节奏
curl -s http://127.0.0.1:8848/healthz           # 健康检查
cd /opt/daily-brief && bash deploy/install.sh   # 更新到最新代码
```

更新代码不会动 `data/brief.db`；迁移历史数据用：

```bash
# 本地导出 -> 服务器导入
sqlite3 daily-brief/data/brief.db ".backup /tmp/brief.db"
scp /tmp/brief.db root@192.3.13.21:/opt/daily-brief/data/brief.db
ssh root@192.3.13.21 'systemctl restart daily-brief-web daily-brief-scheduler'
```

## 七、注意事项

- 采集频率请遵守各上游源的 robots 与服务条款，默认节奏（热搜 2min / 快讯 1min）已与目标站自述一致，可按需调大间隔：`HOT_INTERVAL` / `NEWS_INTERVAL` 环境变量。
- 数据仅作信息聚合，不构成投资建议（沿用目标站同样的免责声明口径）。
- 本项目不包含、不依赖目标站的任何鉴权凭据或付费内容。
- `data/` 目录（数据库、日志、cookie）已在 `.gitignore` 中排除，不会进入版本库。
