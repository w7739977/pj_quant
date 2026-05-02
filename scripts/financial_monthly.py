"""
财务指标每月增量更新

每月 1 号 + 4/8/10 月（季报/中报/三季报披露窗口）跑一次

入口: python3 scripts/financial_monthly.py

crontab:
  0 17 1 * * cd /path/to/pj_quant && python3 scripts/financial_monthly.py >> logs/financial.log 2>&1
"""
import os
import sys
import logging
import pandas as pd
from datetime import datetime

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run_monthly():
    from data.financial_indicator import batch_fetch_all

    end = datetime.now()
    # 仅拉最近 3 个月的新数据
    start = (end.replace(day=1) - pd.DateOffset(months=3)).strftime("%Y%m%d")
    logger.info(f"财务增量更新: {start} ~ 至今")
    batch_fetch_all(start_date=start)


if __name__ == "__main__":
    run_monthly()
