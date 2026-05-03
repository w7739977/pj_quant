"""
项目配置文件
"""

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

# ============ PushPlus 微信推送 ============
PUSHPLUS_TOKEN = "6f113b0c12f84755bb5659319a6ea2c7"
PUSHPLUS_TOKENS = [
    "6f113b0c12f84755bb5659319a6ea2c7",
    "02c977f729cb467cb6641485660c2274",
]

# ============ 定时任务 ============
SIGNAL_RUN_HOUR = 15
SIGNAL_RUN_MINUTE = 30

# ============ 智谱 GLM LLM 配置 ============
# 主源: DeepSeek（OpenAI 兼容 API，¥1/百万 tokens，稳定）
DEEPSEEK_API_KEY = "sk-951903d7ca2b452ba0303c78b0b398f1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

# 备源: GLM-4-flash（智谱 AI，免费）
LLM_API_KEY = "ae6f9312d393475088dd73b65fd3fd0d.I2Tj5lDL5IvexJV4"
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

# ============ Brave Search API ============
BRAVE_API_KEY = "BSA_6qnODLG_U_CLx6z4rlfy9YF-TQh"
BRAVE_BASE_URL = "https://api.search.brave.com/res/v1/web/search"

# ============ 模拟盘参数 ============
SIM_INITIAL_CAPITAL = 500000.0
SIM_DB_PATH = "data/sim_trading.db"
SIM_BAR_INTERVAL = 180     # 盘中轮询间隔(秒)

# ============ 成长路线图 ============
# Phase 1 (已完成): ETF 动量轮动
# Phase 2 (下一步): 股债利差择时 + 可转债双低 + 多指标轮动
# Phase 3 (进阶):   多因子选股 + 大小盘风格轮动
# Phase 4 (高阶):   机器学习因子挖掘 + LLM 情绪分析
