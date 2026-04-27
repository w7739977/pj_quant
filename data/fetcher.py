"""
数据获取模块 — 多数据源自动降级

数据源优先级:
1. 本地 SQLite 缓存
2. 东方财富直连 API (HTTP JSON)
3. AKShare (pip 包)
4. 腾讯财经 (HTTP)
5. BaoStock (TCP)

所有获取的数据自动写入本地缓存，回测/信号优先读缓存。
"""

import pandas as pd
import numpy as np
import requests
import json
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)


# ============ 工具函数 ============

def _code_to_secid(symbol: str) -> str:
    """股票/ETF代码转东方财富 secid 格式: 1.600519(沪) 0.000001(深)"""
    if symbol.startswith("6") or symbol.startswith("5"):
        return f"1.{symbol}"
    return f"0.{symbol}"


def _code_to_prefix(symbol: str) -> str:
    """代码转带市场前缀: sh.600519 / sz.000001"""
    if symbol.startswith("6") or symbol.startswith("5"):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


# ============ 东方财富直连 API ============

def fetch_daily_eastmoney(symbol: str, start_date: str, end_date: str,
                          adjust: str = "qfq") -> pd.DataFrame:
    """
    东方财富直连 K 线历史数据 (HTTP JSON)

    无需 AKShare，直接请求东方财富 API，速度更快。
    """
    secid = _code_to_secid(symbol)
    fqt = {"qfq": 1, "hfq": 2, "": 0}.get(adjust, 1)

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101,  # 日K
        "fqt": fqt,
        "beg": start_date.replace("-", ""),
        "end": end_date.replace("-", ""),
        "lmt": 100000,
    }

    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()

    klines = data.get("data", {}).get("klines", [])
    if not klines:
        return pd.DataFrame()

    rows = []
    for line in klines:
        parts = line.split(",")
        # f51=date, f52=open, f53=close, f54=high, f55=low, f56=volume(手), f57=amount, f58=振幅, f59=涨跌幅, f60=涨跌额, f61=换手率
        rows.append({
            "date": parts[0],
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": float(parts[5]) * 100,  # 手 → 股
            "turnover": float(parts[10]) if len(parts) > 10 else np.nan,
        })

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def fetch_realtime_eastmoney(symbol: str) -> dict:
    """东方财富实时行情快照"""
    secid = _code_to_secid(symbol)
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields": "f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f170",
    }
    try:
        resp = requests.get(url, params=params, timeout=5)
        d = resp.json().get("data", {})
        if not d:
            return {}
        return {
            "symbol": symbol,
            "name": d.get("f58", ""),
            "price": d.get("f43", 0) / 100,
            "change_pct": d.get("f170", 0) / 100,
            "volume": d.get("f47", 0),
            "high": d.get("f44", 0) / 100,
            "low": d.get("f45", 0) / 100,
            "open": d.get("f46", 0) / 100,
        }
    except Exception as e:
        logger.warning(f"东方财富实时行情失败: {e}")
        return {}


# ============ 腾讯财经 API ============

def fetch_realtime_tencent(symbol: str) -> dict:
    """
    腾讯财经实时行情 (51个字段，含 PE/市值/换手率)

    返回字段最丰富的免费实时行情接口。
    """
    prefix = "sh" if (symbol.startswith("6") or symbol.startswith("5")) else "sz"
    code = f"{prefix}{symbol}"
    url = f"http://qt.gtimg.cn/q={code}"

    try:
        resp = requests.get(url, timeout=5)
        text = resp.text.strip()
        if not text or '=""' in text:
            return {}

        # 解析: v_sh600519="1~华能国际~..."
        parts = text.split('"')[1].split("~")
        if len(parts) < 49:
            return {}

        return {
            "symbol": symbol,
            "name": parts[1],
            "price": float(parts[3]),
            "prev_close": float(parts[4]),
            "open": float(parts[5]),
            "volume": float(parts[36]) if parts[36] else 0,  # 股
            "amount": float(parts[37]) if parts[37] else 0,  # 元
            "turnover_rate": float(parts[38]) if parts[38] else 0,
            "pe_ttm": float(parts[39]) if parts[39] else 0,
            "high": float(parts[33]) if parts[33] else 0,
            "low": float(parts[34]) if parts[34] else 0,
            "change_pct": float(parts[32]) if parts[32] else 0,
            "total_market_cap": float(parts[45]) if parts[45] else 0,  # 总市值(万)
            "float_market_cap": float(parts[46]) if parts[46] else 0,  # 流通市值(万)
        }
    except Exception as e:
        logger.warning(f"腾讯实时行情失败: {e}")
        return {}


def fetch_realtime_tencent_batch(symbols: list) -> pd.DataFrame:
    """腾讯批量实时行情（单次最多约100只）"""
    codes = []
    for s in symbols:
        prefix = "sh" if (s.startswith("6") or s.startswith("5")) else "sz"
        codes.append(f"{prefix}{s}")

    url = f"http://qt.gtimg.cn/q={','.join(codes)}"
    try:
        resp = requests.get(url, timeout=10)
        lines = resp.text.strip().split(";")
        rows = []
        for line in lines:
            line = line.strip()
            if '=""' in line or not line:
                continue
            parts = line.split('"')[1].split("~")
            if len(parts) < 49:
                continue
            rows.append({
                "code": parts[2],
                "name": parts[1],
                "price": float(parts[3]) if parts[3] else 0,
                "change_pct": float(parts[32]) if parts[32] else 0,
                "volume": float(parts[36]) if parts[36] else 0,
                "amount": float(parts[37]) if parts[37] else 0,
                "turnover_rate": float(parts[38]) if parts[38] else 0,
                "pe_ttm": float(parts[39]) if parts[39] else 0,
                "total_market_cap": float(parts[45]) if parts[45] else 0,
                "float_market_cap": float(parts[46]) if parts[46] else 0,
            })
        return pd.DataFrame(rows)
    except Exception as e:
        logger.warning(f"腾讯批量行情失败: {e}")
        return pd.DataFrame()


# ============ 新浪财经 API ============

def fetch_realtime_sina(symbols: list) -> pd.DataFrame:
    """
    新浪财经实时行情（含五档盘口）

    适合需要盘口深度的场景。
    """
    codes = []
    for s in symbols:
        prefix = "sh" if (s.startswith("6") or s.startswith("5")) else "sz"
        codes.append(f"{prefix}{s}")

    url = f"http://hq.sinajs.cn/list={','.join(codes)}"
    headers = {"Referer": "https://finance.sina.com.cn"}
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        resp.encoding = "gbk"
        rows = []
        for line in resp.text.strip().split("\n"):
            if '=""' in line or not line:
                continue
            # var hq_str_sh600519="贵州茅台,1794.92,..."
            content = line.split('"')[1]
            if not content:
                continue
            fields = content.split(",")
            if len(fields) < 32:
                continue
            rows.append({
                "code": line.split("=")[0].split("_")[-1].replace("sh", "").replace("sz", ""),
                "name": fields[0],
                "open": float(fields[1]) if fields[1] else 0,
                "prev_close": float(fields[2]) if fields[2] else 0,
                "price": float(fields[3]) if fields[3] else 0,
                "high": float(fields[4]) if fields[4] else 0,
                "low": float(fields[5]) if fields[5] else 0,
                "volume": float(fields[8]) if fields[8] else 0,  # 股
                "amount": float(fields[9]) if fields[9] else 0,  # 元
            })
        return pd.DataFrame(rows)
    except Exception as e:
        logger.warning(f"新浪实时行情失败: {e}")
        return pd.DataFrame()


# ============ AKShare 数据源 ============

def fetch_daily_akshare(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """AKShare 获取日线 (ETF 和 个股通用)"""
    import akshare as ak

    # 先尝试 ETF 接口
    try:
        df = ak.fund_etf_hist_em(symbol=symbol, period="daily",
                                  start_date=start_date.replace("-", ""),
                                  end_date=end_date.replace("-", ""),
                                  adjust="qfq")
        if df is not None and len(df) > 0:
            df = df.rename(columns={
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
            })
            df["date"] = pd.to_datetime(df["date"])
            df = df[["date", "open", "close", "high", "low", "volume"]].copy()
            df = df.sort_values("date").reset_index(drop=True)
            return df
    except Exception:
        pass

    # 个股接口
    try:
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                 start_date=start_date.replace("-", ""),
                                 end_date=end_date.replace("-", ""),
                                 adjust="qfq")
        if df is not None and len(df) > 0:
            df = df.rename(columns={
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
                "换手率": "turnover",
            })
            df["date"] = pd.to_datetime(df["date"])
            cols = ["date", "open", "close", "high", "low", "volume"]
            if "turnover" in df.columns:
                cols.append("turnover")
            df = df[cols].copy()
            df = df.sort_values("date").reset_index(drop=True)
            return df
    except Exception as e:
        logger.warning(f"AKShare 获取失败 {symbol}: {e}")

    return pd.DataFrame()


# ============ BaoStock 数据源 ============

def fetch_daily_baostock(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """BaoStock 获取日线 (无频率限制，适合批量回填)"""
    import baostock as bs
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        lg = bs.login()
    if lg.error_code != "0":
        raise ConnectionError(f"Baostock login failed: {lg.error_msg}")

    try:
        bs_code = _code_to_prefix(symbol)
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,volume,turn,pctChg",
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="2",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            bs.logout()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "turnover", "pct_chg"])
    for c in ["open", "high", "low", "close", "volume", "turnover", "pct_chg"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


# ============ 统一入口（多源自动降级） ============

def fetch_etf_daily(symbol: str, start_date: str, end_date: str = None) -> pd.DataFrame:
    """
    获取日线数据 — 多数据源自动降级

    优先级: 东方财富直连 → AKShare → BaoStock
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # 1. 东方财富直连 (最快、最稳定)
    try:
        df = fetch_daily_eastmoney(symbol, start_date, end_date)
        if len(df) > 0:
            logger.info(f"[东方财富] {symbol}: {len(df)} 条")
            return df
    except Exception as e:
        logger.warning(f"[东方财富] {symbol} 失败: {e}")

    # 2. AKShare
    try:
        df = fetch_daily_akshare(symbol, start_date, end_date)
        if len(df) > 0:
            logger.info(f"[AKShare] {symbol}: {len(df)} 条")
            return df
    except Exception as e:
        logger.warning(f"[AKShare] {symbol} 失败: {e}")

    # 3. BaoStock (最稳定但最慢)
    try:
        df = fetch_daily_baostock(symbol, start_date, end_date)
        if len(df) > 0:
            logger.info(f"[BaoStock] {symbol}: {len(df)} 条")
            return df
    except Exception as e:
        logger.warning(f"[BaoStock] {symbol} 失败: {e}")

    raise RuntimeError(f"所有数据源均获取失败: {symbol}")


def fetch_realtime(symbol: str) -> dict:
    """实时行情 — 腾讯 → 东方财富 → 新浪"""
    result = fetch_realtime_tencent(symbol)
    if result and result.get("price", 0) > 0:
        return result

    result = fetch_realtime_eastmoney(symbol)
    if result and result.get("price", 0) > 0:
        return result

    df = fetch_realtime_sina([symbol])
    if not df.empty:
        return df.iloc[0].to_dict()

    return {}


# ============ 资金流向 ============

def _fmt_flow_amount(wan_yuan: float) -> str:
    """万元 → 可读金额字符串（含正负号）: +1.2亿 / -8000万 / +500万"""
    sign = "+" if wan_yuan >= 0 else "-"
    abs_val = abs(wan_yuan)
    if abs_val >= 10000:
        return f"{sign}{abs_val / 10000:.1f}亿"
    elif abs_val >= 100:
        return f"{sign}{abs_val:.0f}万"
    else:
        return f"{sign}{abs_val:.1f}万"


def _fmt_flow_amount_plain(wan_yuan: float) -> str:
    """万元 → 可读金额字符串（无符号）: 1.2亿 / 8000万 / 500万"""
    abs_val = abs(wan_yuan)
    if abs_val >= 10000:
        return f"{abs_val / 10000:.1f}亿"
    elif abs_val >= 100:
        return f"{abs_val:.0f}万"
    else:
        return f"{abs_val:.1f}万"


def fetch_capital_flow_batch(symbols: list, trade_date: str = None) -> dict:
    """
    批量获取个股资金流向（Tushare 优先，东方财富兜底）

    Parameters
    ----------
    symbols : list of str   股票代码 ['000001', '600519']
    trade_date : str        交易日期 YYYYMMDD，默认最近交易日

    Returns
    -------
    dict: {code: {
        net_mf_amount: float,  # 主力净流入(万元)
        elg_net: float,        # 超大单净流入(万元)
        lg_net: float,         # 大单净流入(万元)
        md_net: float,         # 中单净流入(万元)
        sm_net: float,         # 小单净流入(万元)
    }}
    """
    if not symbols:
        return {}

    # 优先 Tushare
    result = _fetch_capital_flow_tushare(symbols, trade_date)
    if result:
        return result

    # 兜底: 东方财富
    logger.warning("Tushare 资金流向失败，尝试东方财富")
    return _fetch_capital_flow_eastmoney(symbols)


def _fetch_capital_flow_tushare(symbols: list, trade_date: str = None) -> dict:
    """Tushare moneyflow 接口 — 按交易日获取全市场，过滤目标股"""
    try:
        from data.tushare_fundamentals import _init_tushare
        pro = _init_tushare()

        if trade_date is None:
            # 取最近交易日：用 pretrade_date 确保是实际有数据的交易日
            today = datetime.now().strftime("%Y%m%d")
            cal = pro.trade_cal(exchange="SSE", is_open="1",
                                start_date=today, end_date=today)
            if cal is not None and not cal.empty:
                trade_date = cal.iloc[0].get("pretrade_date")
            if not trade_date:
                # 回退：直接用前一个工作日（感知中国法定节假日）
                from datetime import timedelta
                d = datetime.now().date() - timedelta(days=1)
                try:
                    import chinese_calendar
                    while not chinese_calendar.is_workday(d) or d.weekday() >= 5:
                        d -= timedelta(days=1)
                except Exception:
                    # chinese_calendar 不可用降级到周末判断
                    while d.weekday() >= 5:
                        d -= timedelta(days=1)
                trade_date = d.strftime("%Y%m%d")

        df = pro.moneyflow(trade_date=trade_date)
        if df is None or df.empty:
            return {}

        # 构建本地代码 → ts_code 映射
        symbol_set = set(symbols)

        result = {}
        for _, row in df.iterrows():
            code = row["ts_code"].split(".")[0]
            if code not in symbol_set:
                continue

            elg_net = (row.get("buy_elg_amount", 0) or 0) - (row.get("sell_elg_amount", 0) or 0)
            lg_net = (row.get("buy_lg_amount", 0) or 0) - (row.get("sell_lg_amount", 0) or 0)
            md_net = (row.get("buy_md_amount", 0) or 0) - (row.get("sell_md_amount", 0) or 0)
            sm_net = (row.get("buy_sm_amount", 0) or 0) - (row.get("sell_sm_amount", 0) or 0)

            result[code] = {
                "net_mf_amount": float(row.get("net_mf_amount", 0) or 0),
                "elg_net": float(elg_net),
                "lg_net": float(lg_net),
                "md_net": float(md_net),
                "sm_net": float(sm_net),
            }

        logger.info(f"资金流向(Tushare): {len(result)}/{len(symbols)} 只获取成功 ({trade_date})")
        return result

    except Exception as e:
        logger.warning(f"Tushare 资金流向失败: {e}")
        return {}


def _fetch_capital_flow_eastmoney(symbols: list) -> dict:
    """东方财富直连兜底 — 逐只获取最近1天资金流向"""
    result = {}
    for symbol in symbols:
        try:
            secid = _code_to_secid(symbol)
            url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
            params = {
                "secid": secid,
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "klt": 101,
                "lmt": 1,
            }
            resp = requests.get(url, params=params, timeout=5)
            data = resp.json()
            klines = data.get("data", {}).get("klines", [])
            if not klines:
                continue

            # 解析最后一行: 日期,主力净流入,小单,中单,大单,超大单,...
            parts = klines[-1].split(",")
            if len(parts) < 6:
                continue

            main_net = float(parts[1])   # 主力净流入(元)
            sm_net = float(parts[2])     # 小单净流入
            md_net = float(parts[3])     # 中单净流入
            lg_net = float(parts[4])     # 大单净流入
            elg_net = float(parts[5])    # 超大单净流入

            result[symbol] = {
                "net_mf_amount": round(main_net / 10000, 2),  # 元→万元
                "elg_net": round(elg_net / 10000, 2),
                "lg_net": round(lg_net / 10000, 2),
                "md_net": round(md_net / 10000, 2),
                "sm_net": round(sm_net / 10000, 2),
            }
            time.sleep(0.05)  # 避免限流
        except Exception as e:
            logger.debug(f"东方财富资金流向 {symbol} 失败: {e}")
            continue

    if result:
        logger.info(f"资金流向(东方财富): {len(result)}/{len(symbols)} 只获取成功")
    return result


# ============ 保留兼容旧接口 ============

def fetch_etf_realtime(symbol: str) -> dict:
    """获取 ETF 实时行情（兼容旧代码）"""
    return fetch_realtime(symbol)
