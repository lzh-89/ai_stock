# A 股中短线雷达

本项目是一个本地运行的 A 股交易工作台。首页旧“推荐总览”已经下线，不再生成或展示 15m / 日线 Top20 预览；当前首页只保留独立工具入口，具体分析在各自模块中完成。

当前范围：

- 沪深主板 + 创业板；北交所不包含，科创板默认不包含。
- 首页为交易工作台：个股分析、AI 全市场选股、早盘主线雷达、超短线 T+1、个股分析复盘、散户榜、重点池。
- 个股分析：输入股票代码或名称，结合 15m K 线、日 K、基本面、估值、资金流、新闻公告、市场风向和板块热度，输出当前是否适合买入以及短线/中线/长线判断。
- AI 全市场选股：使用独立运行表 `ai_full_market_selection_runs/items`，不复用旧首页推荐表。
- 早盘主线雷达：同时支持行业和概念板块，先计算透明规则活跃度，再由 DeepSeek 受限审查；规则基线和 AI 结果都会落库。
- 超短线 T+1：使用独立运行表和反馈表，面向尾盘隔夜交易辅助。
- SQLite 保存自选股、个股分析快照、AI 全市场选股、T+1、复盘和散户榜相关数据。
- 旧推荐总览链路已移除：不再保留 `/api/analysis/*`、`/api/daily/*`、`/api/review/today`、日线 Top20 回测、Top20 深度分析和推荐缓存表。

## 本地启动

如果要接 AKShare 真实行情，先安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

自动模式主要用于独立模块中的全市场上下文、AI 全市场选股和 T+1 候选池。新浪不可用时会回退到腾讯动态重点池，AKShare / 东方财富作为后置备用诊断源；最后才回退模拟数据。腾讯重点池优先读取每日可更新的 `data/default_universe.json`，没有缓存时才回退代码内置样本。

| 请求数据源 | 用途 | 限制 |
|---|---|---|
| 自动 | 按 Sina、Tencent、AKShare、Tushare、BaoStock、Mock 顺序尝试 | 供独立工具拉取全市场上下文或候选池 |
| 新浪 | 全 A 分页快照 | 默认全市场上下文来源；会显示实际快照数量 |
| AKShare/东财 | 全 A 实时快照 | 当前网络可能无法访问东财行情域名，保留为诊断源 |
| 腾讯 | 动态重点池实时快照 | 不做全 A 扫描，只做重点池兜底 |
| Tushare Pro | 日线、公告、历史数据底座 | 需要 `TUSHARE_TOKEN` |
| BaoStock | 免费历史日线 | 不是盘中实时源 |
| Mock | 本地模拟数据 | 仅用于开发 fallback |

推荐直接运行脚本：

```bash
./run.sh
```

macOS 也可以双击：

```text
run.command
```

或者手动启动：

```bash
python3 -m backend.server
```

启动后打开：

```text
http://127.0.0.1:8000
```

健康检查：

```text
http://127.0.0.1:8000/api/health
```

检查 AKShare 真实行情是否能访问：

```bash
python3 scripts/check_akshare.py
```

如果这个脚本失败，说明当前网络下 AKShare 的东方财富接口不可用；自动模式会跳过到新浪、腾讯等可用真实源，并在扫描状态中标出数据源回退原因。

手动更新腾讯动态重点池：

```bash
python3 scripts/update_default_universe.py --source auto --size 80
```

也可以在页面点击“重点池”后使用弹窗里的“更新”按钮。更新逻辑会优先用新浪全市场快照生成 `data/default_universe.json`，新浪失败时尝试 AKShare；更新失败不会清空旧缓存，腾讯源会继续使用上一次可用重点池。

## AI 配置

AI 当前服务于三个独立能力：

- 个股分析：基于结构化证据包生成买点、周期、仓位和风控判断。
- AI 全市场选股：独立页面分批筛选候选并终选，不写入旧首页推荐表。
- 早盘主线雷达：对规则 Top20 行业/概念板块做受限审查、重排、降级或拒绝，不直接输出买入指令。
- 超短线 T+1：独立页面生成尾盘隔夜候选并进入验证反馈闭环。

旧首页推荐总览的 15m Top20、日线 Top20、推荐缓存、日线推荐回测、Top20 深度分析已经下线；相关脚本、缓存文件、路由和 SQLite 表已清理。`data/daily_kline_cache.json` 仍保留，因为个股分析、AI 全市场和 T+1 仍需要日 K 数据底座。

如果你只有 DeepSeek key，推荐这样配置：

```bash
export AI_PROVIDER="deepseek"
export DEEPSEEK_API_KEY="你的 DeepSeek API Key"
export DEEPSEEK_MODEL="deepseek-v4-flash"
export AI_BASE_URL="https://api.deepseek.com"
```

也可以新建一个 `.env` 文件，`run.sh` 会自动读取：

```bash
AI_PROVIDER="deepseek"
DEEPSEEK_API_KEY="你的 DeepSeek API Key"
DEEPSEEK_MODEL="deepseek-v4-flash"
AI_BASE_URL="https://api.deepseek.com"
```

如果后续要换其他兼容接口：

```bash
AI_PROVIDER="custom"
AI_API_KEY="你的 API Key"
AI_MODEL="模型名"
AI_BASE_URL="https://服务商地址"
```

## 个股案例学习配置

个股分析会保存历史分析快照，并在后续回填 3/5/7/10 日收益。案例学习默认 `auto`：有效样本未达到门槛前只在前端展示，不注入 DeepSeek，不改变当前判断。

可以在 `.env` 配置：

```bash
STOCK_CASE_PROMPT_MODE="auto"              # off / preview / auto / on
STOCK_CASE_PROMPT_MIN_SAMPLES="800"        # 自动注入 prompt 的总有效样本门槛
STOCK_CASE_PROMPT_MIN_SIMILARITY="0.65"    # 相似案例最低相似度
STOCK_CASE_PROMPT_MIN_MATCHED_CASES="5"    # 自动注入所需相似案例数量
STOCK_CASE_RECENT_FALSE_BUY_WINDOW="200"   # 最近误买率统计窗口
STOCK_CASE_FALSE_BUY_ALLOWED_DEGRADE_POINTS="5"
STOCK_CASE_FALSE_BUY_ABSOLUTE_LIMIT_PCT="25"
STOCK_CASE_SUCCESS_3D_RETURN_PCT="3"
STOCK_CASE_SUCCESS_10D_RETURN_PCT="6"
STOCK_CASE_FALSE_BUY_RETURN_PCT="-3"
STOCK_CASE_FALSE_BUY_DRAWDOWN_PCT="-5"
STOCK_CASE_MISSED_5D_RETURN_PCT="5"
STOCK_CASE_DEDUPE_WINDOW_DAYS="5"          # AI复盘/相似案例中，同一股票N天内只取一条代表性案例
```

也可以在 `config/settings.json` 里配置 `stock_case_prompt`，字段参考 `config/settings.example.json`。环境变量优先级高于 `settings.json`。

手动补算个股分析收益：

```bash
./refresh_stock_analysis_outcomes.sh
```

手动生成误买/漏判复盘：

```bash
./review_stock_analysis_failures.sh
```

## Tushare 配置

如果后续要接 Tushare：

```bash
export TUSHARE_TOKEN="你的 Tushare Token"
```

获取方式：登录 Tushare Pro，进入个人中心，在“账号与TOKEN”中复制 token。

## 项目结构

```text
backend/
  server.py                 本地 HTTP API + 静态文件服务
  app/
    database.py             SQLite 表和读写
    settings.py             配置和环境变量
    engines/                过滤、指标、评分、时间窗、复盘
    providers/              模拟数据、AKShare/Tushare 适配入口
    services/               分析、自选股、AI 选股和解释
config/
  settings.example.json     配置示例
data/
  app.sqlite                运行后自动生成
  default_universe.json     动态重点池缓存，更新后生成
docs/
  已确认需求.md 需求、现状与路线图
  数据源说明.md 数据源接入说明
  投资经理能力差距分析.md 投资经理级能力差距分析
  需求确认问题.md 需求问答
  股票AI能力规格.md 规格文档
src/
  app.js                    工作台和工具页面交互
  styles.css                页面样式
index.html                  页面入口
```

## API

- `GET /api/health`
- `GET /api/watchlist`
- `POST /api/watchlist`
- `DELETE /api/watchlist/{code}`
- `GET /api/universe/default`
- `POST /api/universe/default/refresh`
- `GET /api/market-context/latest`
- `POST /api/market-context/refresh`
- `POST /api/stock/analyze`
- `GET /api/stock/search`
- `GET /api/stock/case-memory/stats`
- `POST /api/stock/outcomes/refresh`
- `POST /api/stock/trade-outcomes/refresh`
- `POST /api/stock/review/generate`
- `GET /api/ai-full-market-selection/latest`
- `POST /api/ai-full-market-selection/run`
- `GET /api/morning-sector-radar/latest`
- `GET /api/morning-sector-radar/runs/{runId}`
- `GET /api/morning-sector-radar/diagnostics`
- `POST /api/morning-sector-radar/run`
- `POST /api/morning-sector-radar/outcomes/refresh`
- `GET /api/t1-overnight-selection/latest`
- `POST /api/t1-overnight-selection/run`
- `GET /api/retail-holders/latest`
- `POST /api/retail-holders/refresh`
