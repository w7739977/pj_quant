# A 股量化交易系统 — 云主机部署指南

本文档面向运维工程师，说明在云主机上从零部署 **pj_quant** 到日常运维的全流程。项目仓库：<https://github.com/w7739977/pj_quant.git>

---

## 一、环境准备

### 1. 系统要求

- Python 3.9+
- SQLite3（通常系统自带）
- 约 2GB 磁盘空间（数据库 + Parquet 缓存）

### 2. 拉取项目

```bash
git clone https://github.com/w7739977/pj_quant.git
cd pj_quant
```

### 3. 创建虚拟环境

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. 配置密钥

在项目根目录创建 `.env` 文件（此文件已被 `.gitignore` 排除）：

```env
PUSHPLUS_TOKEN=你的PushPlus微信推送token
LLM_API_KEY=你的智谱GLM API Key
BRAVE_API_KEY=你的Brave Search API Key
WEB_TOKEN=你的Web服务访问token（自定义，用于手机访问鉴权）
```

验证配置是否生效：

```bash
python3 -c "from config.settings import LLM_API_KEY, PUSHPLUS_TOKEN; print('LLM:', bool(LLM_API_KEY), 'Push:', bool(PUSHPLUS_TOKEN))"
```

应输出：`LLM: True Push: True`

### 5. Tushare Token（如需补全基本面数据）

Tushare 的 token 配置在 `data/tushare_fundamentals.py` 中。如需执行基本面数据补全，需修改该文件中的 token 或通过环境变量传入。

---

## 二、数据获取（首次部署必须按顺序执行）

### 步骤 1：ETF 历史数据（约 2 分钟）

```bash
python3 main.py fetch
```

验证：

```bash
python3 -c "
from data.storage import load_daily_data
for etf in ['510300','510500','159915','513100','511010']:
    df = load_daily_data(etf)
    print(f'{etf}: {len(df)} 条, 最新 {df[\"date\"].max()}')"
```

预期：每个 ETF 有 1000+ 条数据，最新日期接近今天。

### 步骤 2：全 A 股日线数据

**推荐方案：Tushare（约 30-50 分钟，K线 + 基本面一键完成）**

```bash
python3 main.py fetch-all --tushare
# 限量测试: python3 main.py fetch-all --tushare --limit 10
```

> 该命令自动完成 K 线下载 + 基本面补全（pe_ttm, pb, turnover_rate, volume_ratio）。

**备选方案：BaoStock + 手动补全基本面（约 4-5 小时）**

```bash
python3 main.py fetch-all
python3 -c "from data.tushare_fundamentals import run; run()"
```

验证（K 线 + 基本面都要检查）：

```bash
python3 -c "
from data.storage import list_cached_stocks, load_stock_daily
stocks = list_cached_stocks()
print(f'已缓存: {len(stocks)} 只股票')
df = load_stock_daily('000001')
for c in ['pe_ttm','pb','turnover_rate','volume_ratio']:
    print(f'  {c}: {df[c].notna().mean():.0%}')
"
```

预期：4400+ 只股票，pe_ttm > 80%，其余 > 99%。**如果基本面全为 0%，模型训练会过拟合（R² 虚高到 0.9+）。**

### 步骤 3：基本面数据补全（仅 BaoStock 方案需要单独执行）

```bash
python3 -c "from data.tushare_fundamentals import run; run()"
```

如需限量测试：

```bash
python3 -c "from data.tushare_fundamentals import run; run(limit=5)"
```

验证：

```bash
python3 -c "
from data.storage import load_stock_daily
df = load_stock_daily('000001')
print('列:', list(df.columns))
print('pe_ttm 非空率:', df['pe_ttm'].notna().mean())"
```

预期：列中包含 `pe_ttm`, `pb`, `turnover_rate`, `volume_ratio`，非空率 > 80%。

---

## 三、数据验证

运行 preflight 健康检查（验证数据新鲜度/准确性/完整性/模型状态）：

```bash
python3 scripts/preflight.py
```

预期输出（首次部署模型还没训练，模型检查会失败）：

- `[✓]` 数据新鲜度: 10/10 只股票数据最新
- `[✓]` 数据准确性: 3/3 只对比通过
- `[✓]` 数据完整性: pe_ttm 88%, pb 99%, turnover_rate 99%
- `[✗]` 模型状态: 模型文件不存在

模型检查失败是正常的，下一步训练后会通过。

---

## 四、模型训练

训练 XGBoost 选股模型（约 5-15 分钟，取决于数据量）：

```bash
python3 main.py train
```

预期输出包含：

- 训练样本数: 80000+ 条
- 交叉验证 R²: > 0.02（通常 0.05-0.10）
- 特征重要性排名
- 模型保存到 `ml/models/xgb_ranker.json`

验证：

```bash
python3 -c "
from ml.ranker import get_model_info
info = get_model_info()
print('模型存在:', info['has_model'])
print('R²:', info['current'].get('cv_r2_mean'))
print('训练样本:', info['current'].get('train_samples'))
print('Top 因子:', info['current'].get('top_factors', [])[:5])"
```

训练完成后再次运行 preflight，应全部通过：

```bash
python3 scripts/preflight.py
```

---

## 五、功能验证（逐个测试核心命令）

### 1. 选股预测

```bash
python3 main.py predict
```

预期：输出 Top 10 股票及预测收益率排名。

### 2. 市场情绪分析

```bash
python3 main.py sentiment
```

预期：输出综合情绪分数、新闻摘要、GLM 分析结果。

### 3. 实盘建议生成（不推送，仅本地查看）

```bash
python3 main.py live
```

预期：输出持仓检查 + 卖出建议 + 买入建议 + 操作清单。

### 4. 实盘建议 + 微信推送

```bash
python3 main.py live --push
```

预期：同上，并收到微信推送消息。

### 5. 持仓管理

```bash
python3 main.py portfolio                                    # 查看持仓
python3 main.py portfolio --reset                            # 重置为初始状态（20000元）
python3 main.py portfolio --buy 000001 --shares 100 --price 10.5  # 模拟买入
python3 main.py portfolio --sell 000001 --price 11.0              # 模拟卖出
```

### 6. 信号归档

```bash
python3 scripts/postflight.py
cat logs/signals/$(date +%Y-%m-%d).json | python3 -m json.tool
```

预期：生成今日信号 JSON 文件。

### 7. 绩效追踪（首次无数据，会提示不足）

```bash
python3 main.py performance
```

预期：提示信号数据积累不足，运行 20 个交易日后再查看。

### 8. 运行全部测试

```bash
python3 -m pytest tests/ -v
```

预期：57 个测试全部通过。

---

## 六、Web 持仓同步服务

### 1. 启动服务

```bash
python3 server.py --port 8080 &
```

或用 nohup 后台运行：

```bash
nohup python3 server.py --port 8080 > logs/server.log 2>&1 &
```

### 2. 测试访问

```bash
curl -s "http://localhost:8080/api/status?token=你的WEB_TOKEN" | python3 -m json.tool
```

### 3. 手机访问

浏览器打开：`http://云主机IP:8080/?token=你的WEB_TOKEN`

### 4. 如需 HTTPS + 域名，配置 nginx 反向代理

```nginx
server {
    listen 443 ssl;
    server_name your.domain.com;
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    location / {
        proxy_pass http://127.0.0.1:8080;
    }
}
```

### 5. 用 systemd 管理服务（可选，推荐）

创建 `/etc/systemd/system/pj-quant-web.service`：

```ini
[Unit]
Description=pj_quant Web Sync Service
After=network.target

[Service]
Type=simple
User=你的用户名
WorkingDirectory=/path/to/pj_quant
ExecStart=/path/to/pj_quant/venv/bin/python3 server.py --port 8080
Restart=always
RestartSec=5
Environment=WEB_TOKEN=你的token

[Install]
WantedBy=multi-user.target
```

然后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable pj-quant-web
sudo systemctl start pj-quant-web
```

---

## 七、定时任务配置

编辑 crontab：

```bash
crontab -e
```

添加以下内容（注意替换路径）：

```cron
# 每日收盘后运行（周一至周五 15:30）
# 流程: preflight检查 → 生成建议推送微信 → 归档信号
30 15 * * 1-5 cd /path/to/pj_quant && /path/to/pj_quant/venv/bin/python3 -c "pass" && bash run_daily.sh >> logs/cron.log 2>&1

# 每月模型自动进化（每月1号 16:00）
0 16 1 * * cd /path/to/pj_quant && source venv/bin/activate && python3 main.py evolve --push >> logs/evolve.log 2>&1
```

注意 cron 中需要激活 venv，或在 `run_daily.sh` 开头添加：

```bash
source /path/to/pj_quant/venv/bin/activate
```

验证 cron 是否生效：

```bash
crontab -l
```

---

## 八、日常运维流程

**每日自动流程（无需人工干预）：**

15:30 cron → preflight 检查 → live --push → postflight 归档 → 你收到微信推送的操作建议。

**你的操作流程：**

1. 收到微信推送，决定是否执行建议。
2. 实际操作后，手机打开 Web 页面同步持仓：`http://云主机IP:8080/?token=xxx`
3. 勾选已执行的操作，或手动补充，点击「确认同步」。
4. 次日系统基于真实持仓生成新建议。

**如果某天没操作：**

- 不需要做任何事，持仓不变。
- 次日系统基于当前真实持仓继续推送建议。

**定期维护：**

- 每月 1 号自动 evolve（模型进化）。
- 积累 20 个交易日后查看绩效：`python3 main.py performance`
- 查看进化历史：`python3 main.py evolve-history`

---

## 九、故障排查

### 1. preflight 失败告警

查看日志：

```bash
cat logs/daily_$(date +%Y-%m-%d).log
```

常见原因：

- 数据不新鲜 → 手动运行 `python3 main.py fetch-all --refresh`
- 模型过期 → 手动运行 `python3 main.py evolve`
- 网络问题 → 检查云主机出网

### 2. live 执行失败

查看日志同上。

常见原因：

- 行情接口限流 → 等待几分钟重试
- GLM API 余额不足 → 检查智谱账户

### 3. Web 服务不可达

```bash
ps aux | grep server.py
ss -tlnp | grep 8080
cat logs/server.log
```

### 4. 微信推送没收到

验证 token：

```bash
python3 -c "
from alert.notify import send_message
from config.settings import PUSHPLUS_TOKEN
send_message('测试', '部署验证', PUSHPLUS_TOKEN)"
```

---

## 十、完整命令速查

**数据：**

| 命令 | 说明 |
|------|------|
| `python3 main.py fetch` | ETF 数据 |
| `python3 main.py fetch-all` | 全 A 股日线 |
| `python3 main.py fetch-all --limit 100` | 限量测试 |

**策略：**

| 命令 | 说明 |
|------|------|
| `python3 main.py live [--push]` | 实盘建议 |
| `python3 main.py deploy [--push]` | 标准部署(ETF+个股) |
| `python3 main.py predict` | ML 选股预测 |
| `python3 main.py smallcap` | 小盘多因子选股 |
| `python3 main.py sentiment` | 情绪分析 |
| `python3 main.py backtest` | ETF 回测 |

**模型：**

| 命令 | 说明 |
|------|------|
| `python3 main.py train` | 训练模型 |
| `python3 main.py evolve [--push]` | 模型进化 |
| `python3 main.py evolve-history` | 进化历史 |

**持仓：**

| 命令 | 说明 |
|------|------|
| `python3 main.py portfolio` | 查看持仓 |
| `python3 main.py portfolio --reset` | 重置 |
| `python3 main.py portfolio --buy CODE --shares N --price X` | 模拟买入 |
| `python3 main.py portfolio --sell CODE --price X` | 模拟卖出 |

**运维：**

| 命令 | 说明 |
|------|------|
| `python3 scripts/preflight.py` | 健康检查 |
| `python3 scripts/postflight.py` | 信号归档 |
| `python3 main.py performance [--push]` | 绩效报告 |
| `python3 server.py --port 8080` | Web 服务 |
| `python3 -m pytest tests/ -v` | 运行测试 |

---

本指南覆盖从零部署到日常运维的全流程；每一步均附有验证命令与预期输出。执行时可逐步对照，自行确认各环节是否正常。
