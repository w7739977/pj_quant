"""项目配置 — 所有 secret 从环境变量读取

部署:
  1. cp .env.example .env
  2. 编辑 .env 填入各 API key
  3. cron 入口 / shell 启动前 source .env
     或用 systemd EnvironmentFile=/opt/pj_quant/.env
"""
import os

# 自动加载项目根目录的 .env (如存在) — 简化本地开发体验
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.isfile(_ENV_PATH):
    try:
        with open(_ENV_PATH) as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#"):
                    continue
                if "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                _k = _k.strip()
                _v = _v.strip().strip('"').strip("'")
                # 仅当环境变量未设置时填入（环境变量优先级最高）
                os.environ.setdefault(_k, _v)
    except Exception:
        pass


# ============ 数据库 ============
DB_PATH = "data/quant.db"

# ============ ETF 轮动策略标的 ============
ETF_POOL = {
    "510300": "沪深300ETF",
    "510500": "中证500ETF",
    "159915": "创业板ETF",
    "513100": "纳指100ETF",
    "511010": "国债ETF",
}

# ============ 交易成本 ============
COMMISSION_RATE = 0.0001
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.001
TRANSFER_FEE_RATE = 0.00001

# ============ 实盘交易成本 ============
LIVE_COMMISSION_RATE = 0.00025
LIVE_MIN_COMMISSION = 5.0
LIVE_STAMP_TAX_RATE = 0.001
LIVE_TRANSFER_FEE_RATE = 0.00001

# ============ 回测参数 ============
INITIAL_CAPITAL = 20000.0
BACKTEST_START = "2020-01-01"
BACKTEST_END = "2025-12-31"

# ============ 策略参数 ============
MOMENTUM_LOOKBACK_DAYS = 20
REBALANCE_DAYS = 40
NUM_POSITIONS = 10

# ============ 止损止盈参数 ============
STOP_LOSS_PCT = -0.08
TAKE_PROFIT_PCT = 0.15
MAX_HOLDING_DAYS = 20

# ============ 定时任务 ============
SIGNAL_RUN_HOUR = 15
SIGNAL_RUN_MINUTE = 30

# ============ 模拟盘参数 ============
SIM_INITIAL_CAPITAL = 500000.0
SIM_DB_PATH = "data/sim_trading.db"
SIM_BAR_INTERVAL = 180     # 盘中轮询间隔(秒)


# ============ Secrets — 从环境变量读取 ============

# Tushare（数据源）
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")

# PushPlus 微信推送
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
# 多账号推送：环境变量 PUSHPLUS_TOKENS=token1,token2,token3
_pushplus_multi = os.getenv("PUSHPLUS_TOKENS", "")
PUSHPLUS_TOKENS = [t.strip() for t in _pushplus_multi.split(",") if t.strip()] \
    or ([PUSHPLUS_TOKEN] if PUSHPLUS_TOKEN else [])

# Web 持仓同步服务
WEB_TOKEN = os.getenv("WEB_TOKEN", "pj_quant_2026")  # 非 secret，默认值 OK

# Brave Search API
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
BRAVE_BASE_URL = "https://api.search.brave.com/res/v1/web/search"


# ============ LLM Providers（DeepSeek 主，GLM 备）============

# DeepSeek（OpenAI 兼容，¥1/百万 tokens，稳定）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

# GLM-4-flash（智谱 AI，免费）— 备源
LLM_API_KEY = os.getenv("GLM_API_KEY", "")
LLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
LLM_MODEL = "glm-4-flash"

# LLM provider 列表（按优先级，主家失败自动 fallback）
# sentiment/llm_client.py:chat_completion() 按此顺序依次尝试
LLM_PROVIDERS = [
    {
        "name": "deepseek",
        "url": DEEPSEEK_BASE_URL,
        "key": DEEPSEEK_API_KEY,
        "model": DEEPSEEK_MODEL,
    },
    {
        "name": "glm",
        "url": LLM_BASE_URL,
        "key": LLM_API_KEY,
        "model": LLM_MODEL,
    },
]
