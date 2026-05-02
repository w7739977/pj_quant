"""
因子计算引擎

从原始行情/基本面数据中计算各类因子，用于选股打分。

因子分类:
- 动量因子: 过去N日涨幅
- 波动率因子: 过去N日收益率标准差
- 换手率因子: 平均换手率
- 量价因子: 量价背离、放量程度
- 基本面因子: PE、PB、市值
- 技术因子: MA、RSI、MACD
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from factors.data_loader import get_small_cap_stocks
from data.storage import load_stock_daily


def calc_momentum(df: pd.DataFrame, periods: list = [5, 10, 20, 60]) -> dict:
    """动量因子: 过去N日涨幅"""
    result = {}
    close = df["close"].values
    for p in periods:
        if len(close) > p:
            result[f"mom_{p}d"] = (close[-1] / close[-p - 1] - 1.0)
        else:
            result[f"mom_{p}d"] = np.nan
    return result


def calc_volatility(df: pd.DataFrame, periods: list = [10, 20]) -> dict:
    """波动率因子: 过去N日日收益率标准差"""
    result = {}
    returns = df["close"].pct_change().dropna()
    for p in periods:
        if len(returns) >= p:
            result[f"vol_{p}d"] = returns.tail(p).std()
        else:
            result[f"vol_{p}d"] = np.nan
    return result


def calc_turnover_factor(df: pd.DataFrame, periods: list = [5, 20]) -> dict:
    """换手率因子: 平均换手率、换手率变化"""
    result = {}
    turnover = df.get("turnover", pd.Series(dtype=float))
    if len(turnover) == 0:
        return {f"avg_turnover_{p}d": np.nan for p in periods}

    for p in periods:
        if len(turnover) >= p:
            result[f"avg_turnover_{p}d"] = turnover.tail(p).mean()
        else:
            result[f"avg_turnover_{p}d"] = np.nan

    # 换手率变化（最近5日 vs 之前15日）
    if len(turnover) >= 20:
        recent = turnover.tail(5).mean()
        prior = turnover.iloc[-20:-5].mean()
        result["turnover_accel"] = recent / prior - 1 if prior > 0 else np.nan
    else:
        result["turnover_accel"] = np.nan
    return result


def calc_volume_price(df: pd.DataFrame) -> dict:
    """量价因子: 量价背离、放量程度"""
    result = {}
    if len(df) < 10:
        return {"vol_price_diverge": np.nan, "volume_surge": np.nan}

    # 最近5日 vs 之前15日的量价关系
    recent_ret = df["close"].iloc[-5:].pct_change().dropna().mean()
    prior_ret = df["close"].iloc[-20:-5].pct_change().dropna().mean() if len(df) >= 20 else 0

    recent_vol = df["volume"].iloc[-5:].mean()
    prior_vol = df["volume"].iloc[-20:-5].mean() if len(df) >= 20 else df["volume"].mean()

    # 量价背离: 价格涨但量缩 → 可能见顶
    if recent_vol > 0 and prior_vol > 0:
        result["vol_price_diverge"] = (recent_ret - prior_ret) - (recent_vol / prior_vol - 1)
    else:
        result["vol_price_diverge"] = np.nan

    # 放量程度
    if prior_vol > 0:
        result["volume_surge"] = recent_vol / prior_vol
    else:
        result["volume_surge"] = np.nan
    return result


def calc_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """RSI 因子"""
    close = df["close"]
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    if len(gain) < period:
        return np.nan

    avg_gain = gain.tail(period).mean()
    avg_loss = loss.tail(period).mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_technical(df: pd.DataFrame) -> dict:
    """技术因子: MA偏离度、RSI"""
    result = {}
    close = df["close"]

    # MA 偏离度
    for p in [5, 10, 20]:
        if len(close) >= p:
            ma = close.tail(p).mean()
            result[f"ma{p}_bias"] = (close.iloc[-1] / ma - 1.0)
        else:
            result[f"ma{p}_bias"] = np.nan

    # RSI
    result["rsi_14"] = calc_rsi(df, 14)

    return result


def calc_sentiment_factor(symbol: str) -> dict:
    """
    情绪因子: 基于个股相关新闻的 LLM 情绪分数

    Returns
    -------
    dict: {sentiment_score, sentiment_count}
    """
    try:
        from sentiment.analyzer import flash_tag_sentiment, fetch_stock_news

        news = fetch_stock_news(symbol)
        if not news:
            return {"sentiment_score": 0.0, "sentiment_count": 0}

        titles = [n["title"] for n in news]
        scores = flash_tag_sentiment(titles)

        avg = float(np.mean(scores))
        return {
            "sentiment_score": round(avg, 3),
            "sentiment_count": len(scores),
        }
    except Exception as e:
        logger.warning(f"情绪因子计算失败 {symbol}: {e}")
        return {"sentiment_score": 0.0, "sentiment_count": 0}


def compute_all_factors(symbol: str, end_date: str = None, lookback: int = 120) -> dict:
    """
    计算一只股票的全部因子

    Returns
    -------
    dict: {factor_name: value}
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    start = (pd.to_datetime(end_date) - timedelta(days=lookback * 2)).strftime("%Y-%m-%d")

    # 直接读本地SQLite，不做网络fallback（避免BaoStock连接失败阻塞）
    df = load_stock_daily(symbol)
    if df is None or df.empty or len(df) < 20:
        return {}
    # 按日期过滤
    df = df[(df["date"] >= start) & (df["date"] <= end_date)]
    if len(df) < 20:
        return {}

    factors = {"code": symbol}
    factors.update(calc_momentum(df))
    factors.update(calc_volatility(df))
    factors.update(calc_turnover_factor(df))
    factors.update(calc_volume_price(df))
    factors.update(calc_technical(df))

    # 基本面因子：直接从本地SQLite读取，避免收盘后调用腾讯API
    last_row = df.iloc[-1]
    for col in ["pe_ttm", "pb", "turnover_rate", "volume_ratio"]:
        factors[col] = last_row.get(col, np.nan)

    return factors


def _batch_sentiment_factors(factor_df: pd.DataFrame) -> pd.DataFrame:
    """
    批量计算情绪因子

    策略: 对每只股票抓取新闻标题，然后按批次（每批20只）调用 flash 打标。
    无新闻的股票默认 0 分。
    """
    from sentiment.analyzer import fetch_stock_news, _call_llm, _parse_scores

    df = factor_df.copy()
    df["sentiment_score"] = 0.0
    df["sentiment_count"] = 0

    symbols = df["code"].tolist()
    # 收集所有新闻标题
    stock_titles = {}  # symbol -> [titles]
    for sym in symbols:
        try:
            news = fetch_stock_news(sym)
            if news:
                stock_titles[sym] = [n["title"] for n in news]
        except Exception:
            pass

    if not stock_titles:
        logger.info("无可用的个股新闻，情绪因子全部为 0")
        return df

    # 按批次调用 flash 打标（每批最多 20 只股票的新闻标题）
    batch_size = 20
    all_syms = list(stock_titles.keys())
    for start in range(0, len(all_syms), batch_size):
        batch = all_syms[start:start + batch_size]
        lines = []
        sym_order = []
        for sym in batch:
            for title in stock_titles[sym][:3]:  # 每只最多取3条
                lines.append(f"[{sym}] {title[:60]}")
                sym_order.append(sym)

        if not lines:
            continue

        prompt = f"""给以下{len(lines)}条A股个股新闻打情绪分，范围[-1,1]，-1最利空，1最利好。
每行开头[代码]表示对应的股票。

新闻列表:
{chr(10).join(f'{i+1}. {l}' for i, l in enumerate(lines))}

只回复JSON数组，如 [0.5, -0.3, ...]，不要其他内容。"""

        content = _call_llm("glm-4-flash", prompt, max_tokens=500, temperature=0.3)
        scores = _parse_scores(content, len(lines))

        if scores is None:
            continue

        # 按股票聚合分数
        from collections import defaultdict
        sym_scores = defaultdict(list)
        for sym, sc in zip(sym_order, scores):
            sym_scores[sym].append(sc)

        for sym, sc_list in sym_scores.items():
            idx = df.index[df["code"] == sym]
            if len(idx) > 0:
                df.loc[idx[0], "sentiment_score"] = round(float(np.mean(sc_list)), 3)
                df.loc[idx[0], "sentiment_count"] = len(sc_list)

        logger.info(f"情绪因子批次: {start+1}~{min(start+batch_size, len(all_syms))}/{len(all_syms)} 完成")

    has_sentiment = (df["sentiment_count"] > 0).sum()
    logger.info(f"情绪因子完成: {has_sentiment}/{len(df)} 只有新闻数据")
    return df


def compute_stock_pool_factors(
    min_cap: float = 5e8,
    max_cap: float = 1e13,
    end_date: str = None,
    skip_sentiment: bool = False,
) -> pd.DataFrame:
    """
    计算股票池的因子矩阵（默认覆盖 5 亿以上全市场）

    Returns
    -------
    DataFrame: 每行一只股票，每列一个因子
    """
    pool = get_small_cap_stocks(min_cap, max_cap)
    if pool.empty:
        return pd.DataFrame()

    symbols = pool["code"].tolist()
    logger.info(f"开始计算 {len(symbols)} 只股票的因子...")

    # 逐只计算因子（基本面因子已从本地SQLite读取，无需网络请求）
    all_factors = []
    for i, sym in enumerate(symbols):
        try:
            f = compute_all_factors(sym, end_date)
            if f:
                all_factors.append(f)

                if (i + 1) % 50 == 0:
                    logger.info(f"  已计算 {i+1}/{len(symbols)}")
        except Exception as e:
            logger.warning(f"  {sym} 因子计算失败: {e}")

    df = pd.DataFrame(all_factors)
    if df.empty:
        return df

    # === 注入行业字段 ===
    from data.tushare_industry import get_industry_for_codes
    industry_map = get_industry_for_codes(df["code"].tolist())
    df["industry"] = df["code"].map(industry_map).fillna("未知")

    # 情绪因子: 批量获取个股新闻标题，一次性让 flash 打标
    if skip_sentiment:
        df["sentiment_score"] = np.nan
        logger.info(f"跳过情绪因子计算")
    else:
        logger.info(f"开始计算情绪因子 ({len(df)} 只股票)...")
        df = _batch_sentiment_factors(df)

    logger.info(f"因子计算完成: {len(df)} 只股票, {len(df.columns)} 个因子")
    return df


import logging
logger = logging.getLogger(__name__)


# ============ 因子预处理工具 ============

def winsorize_cross_section(df: pd.DataFrame, cols: list,
                            lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    """
    极值处理（Qlib 标准）— 按截面 1%/99% 分位数 winsorize

    防止异常值（停牌/重组复牌）拖偏 ML 训练
    注意：默认假设 df 已是单一截面（同一 date 的所有股票）
    """
    df = df.copy()
    for col in cols:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        if s.notna().sum() < 10:
            continue
        lo, hi = s.quantile([lower, upper])
        df[col] = s.clip(lower=lo, upper=hi)
    return df


def cross_sectional_zscore(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    """
    截面 Z-score 标准化（Qlib CSZScoreNorm 等价实现）

    每个因子 (x - mean) / std，让所有因子量级一致
    注意：df 必须是单一截面
    """
    df = df.copy()
    for col in cols:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        if s.notna().sum() < 10:
            continue
        m, sd = s.mean(), s.std()
        if sd > 1e-8:
            df[col] = (s - m) / sd
        else:
            df[col] = 0.0
    return df


def industry_neutralize(df: pd.DataFrame, cols: list,
                        industry_col: str = "industry") -> pd.DataFrame:
    """
    行业中性化（信达金工/中金标配）— 按行业分组排名归一化到 0~1

    保留行业内相对优势，去除行业 beta
    """
    if industry_col not in df.columns:
        # 无行业字段，跳过中性化
        return df
    df = df.copy()
    for col in cols:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        # 按行业分组排名（pct=True 得到 0-1 分位数）
        df[col] = s.groupby(df[industry_col]).rank(pct=True, na_option="keep")
    return df


def neutralize_factors(df: pd.DataFrame, factor_cols: list,
                       industry_col: str = "industry") -> pd.DataFrame:
    """
    一站式因子预处理: winsorize → zscore → industry_neutralize

    用于训练数据生成 + 实时预测前
    """
    df = winsorize_cross_section(df, factor_cols)
    df = cross_sectional_zscore(df, factor_cols)
    df = industry_neutralize(df, factor_cols, industry_col)
    return df


def neutralize_factors_per_section(df: pd.DataFrame, factor_cols: list,
                                    section_col: str = "end_date",
                                    industry_col: str = "industry") -> pd.DataFrame:
    """
    按截面分组做中性化（Qlib CSZScoreNorm 标准做法）

    每个 section_col 唯一值（即一个交易日）单独执行：
      winsorize → cross_sectional_zscore → industry_neutralize

    Parameters
    ----------
    df : 含 section_col 字段的训练样本
    factor_cols : 待中性化的因子列
    section_col : 截面分组列（默认 "end_date"）
    industry_col : 行业列（默认 "industry"）

    Returns
    -------
    DataFrame: 同 shape，但因子列已被按截面中性化
    """
    if section_col not in df.columns:
        # 退化为一次性中性化（向后兼容，但应避免使用）
        logger.warning(
            f"无 {section_col} 列，退化为全局中性化（不推荐）"
        )
        return neutralize_factors(df, factor_cols, industry_col)

    df = df.copy()
    # 预转 float 避免 int→float 赋值 FutureWarning
    for col in factor_cols:
        if col in df.columns:
            df[col] = df[col].astype(float)
    sections = df[section_col].unique()

    # 按截面循环，每个截面独立做完整流程
    for section in sections:
        mask = df[section_col] == section
        sub = df.loc[mask].copy()
        # 在该截面内做 winsorize + zscore + industry_rank
        sub = winsorize_cross_section(sub, factor_cols)
        sub = cross_sectional_zscore(sub, factor_cols)
        sub = industry_neutralize(sub, factor_cols, industry_col)
        # 写回原 df
        df.loc[mask, factor_cols] = sub[factor_cols].values

    return df
