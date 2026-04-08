#!/usr/bin/env python3
"""
FastAPI 持仓同步服务 — 手机浏览器操作持仓

启动: python3 server.py [--port 8080]
API:
  GET  /              主页面（HTML）
  GET  /api/status    当前持仓 + 今日建议
  POST /api/sync      同步持仓（买入/卖出）
  GET  /api/preview   预览次日选股建议
"""

import sys
import os
import json
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI(title="pj_quant 持仓同步")

WEB_TOKEN = os.getenv("WEB_TOKEN", "pj_quant_2026")


# ============ 鉴权 ============

def _check_token(request: Request):
    token = request.query_params.get("token") or request.headers.get("X-Token", "")
    if token != WEB_TOKEN:
        raise HTTPException(status_code=401, detail="未授权")


# ============ Models ============

class BuyItem(BaseModel):
    code: str
    shares: int
    price: float

class SellItem(BaseModel):
    code: str
    price: float

class SyncBody(BaseModel):
    executed_buys: List[BuyItem] = []
    executed_sells: List[SellItem] = []


# ============ API ============

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    _check_token(request)
    return _render_homepage()


@app.get("/api/status")
async def status(request: Request):
    _check_token(request)
    return _get_status()


@app.post("/api/sync")
async def sync(body: SyncBody, request: Request):
    _check_token(request)
    return _do_sync(body)


@app.get("/api/preview")
async def preview(request: Request):
    _check_token(request)
    return _get_preview()


# ============ Logic ============

def _get_status() -> dict:
    from portfolio.tracker import PortfolioTracker
    from config.settings import INITIAL_CAPITAL

    tracker = PortfolioTracker()
    holdings = {}
    price_map = {}

    # 获取实时价格
    if tracker.holdings:
        try:
            from data.fetcher import fetch_realtime_tencent_batch
            codes = list(tracker.holdings.keys())
            rt_df = fetch_realtime_tencent_batch(codes)
            for _, row in rt_df.iterrows():
                price_map[row["code"]] = float(row.get("price", 0))
        except Exception:
            pass

    total_value = tracker.cash
    for code, info in tracker.holdings.items():
        price = price_map.get(code, info["avg_cost"])
        market_val = info["shares"] * price
        pnl = (price - info["avg_cost"]) * info["shares"]
        pnl_pct = (price / info["avg_cost"] - 1) * 100 if info["avg_cost"] > 0 else 0
        holdings[code] = {
            "shares": info["shares"],
            "avg_cost": info["avg_cost"],
            "current_price": price,
            "market_value": round(market_val, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 1),
            "buy_date": info.get("buy_date", ""),
        }
        total_value += market_val

    # 今日建议
    today = datetime.now().strftime("%Y-%m-%d")
    signals_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "logs", "signals", f"{today}.json"
    )
    today_signals = None
    if os.path.exists(signals_path):
        try:
            with open(signals_path, "r", encoding="utf-8") as f:
                today_signals = json.load(f)
        except Exception:
            pass

    # 最近同步记录
    sync_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "logs", "sync", f"{today}.json"
    )
    last_sync = None
    if os.path.exists(sync_path):
        try:
            with open(sync_path, "r", encoding="utf-8") as f:
                sync_data = json.load(f)
                last_sync = sync_data.get("timestamp")
        except Exception:
            pass

    return {
        "cash": round(tracker.cash, 2),
        "holdings": holdings,
        "total_value": round(total_value, 2),
        "total_pnl": round(total_value - INITIAL_CAPITAL, 2),
        "total_pnl_pct": round((total_value / INITIAL_CAPITAL - 1) * 100, 1),
        "today_signals": today_signals,
        "last_sync": last_sync,
    }


def _do_sync(body: SyncBody) -> dict:
    from portfolio.tracker import PortfolioTracker
    from portfolio.trade_utils import estimate_buy_cost, estimate_sell_cost

    tracker = PortfolioTracker()
    errors = []
    results = []

    # 记录同步
    today = datetime.now().strftime("%Y-%m-%d")
    sync_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "sync")
    os.makedirs(sync_dir, exist_ok=True)
    sync_path = os.path.join(sync_dir, f"{today}.json")

    warning = None
    if os.path.exists(sync_path):
        warning = "今日已有同步记录，本次为追加操作"

    # 先卖后买
    for item in body.executed_sells:
        try:
            shares = tracker.holdings.get(item.code, {}).get("shares", 0)
            cost = estimate_sell_cost(item.price * shares)
            ok = tracker.update_after_sell(item.code, item.price, cost)
            if ok:
                results.append(f"卖出 {item.code} {shares}股@{item.price:.2f}")
            else:
                errors.append(f"卖出 {item.code} 失败: 不在持仓中")
        except Exception as e:
            errors.append(f"卖出 {item.code} 异常: {e}")

    for item in body.executed_buys:
        try:
            cost = estimate_buy_cost(item.price * item.shares)
            tracker.update_after_buy(item.code, item.shares, item.price, cost)
            results.append(f"买入 {item.code} {item.shares}股@{item.price:.2f}")
        except Exception as e:
            errors.append(f"买入 {item.code} 异常: {e}")

    # 保存同步记录
    sync_record = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "buys": [item.model_dump() for item in body.executed_buys],
        "sells": [item.model_dump() for item in body.executed_sells],
        "results": results,
        "errors": errors,
    }
    with open(sync_path, "w", encoding="utf-8") as f:
        json.dump(sync_record, f, ensure_ascii=False, indent=2)

    response = {
        "success": len(errors) == 0,
        "results": results,
        "errors": errors,
        "status": _get_status(),
    }
    if warning:
        response["warning"] = warning
    return response


def _get_preview() -> dict:
    from portfolio.tracker import PortfolioTracker
    from portfolio.allocator import get_stock_picks_live
    from config.settings import NUM_POSITIONS

    tracker = PortfolioTracker()
    exclude_codes = list(tracker.holdings.keys())
    slots = max(0, NUM_POSITIONS - len(tracker.holdings))

    if slots == 0:
        return {"message": "仓位已满，无可用槽位", "picks": []}

    if tracker.cash < 5000:
        return {"message": f"可用资金不足 ({tracker.cash:,.0f}元)", "picks": []}

    try:
        picks = get_stock_picks_live(
            stock_capital=tracker.cash,
            top_n=slots,
            exclude_codes=exclude_codes,
        )
        return {"picks": picks, "message": f"选出 {len(picks)} 只"}
    except Exception as e:
        return {"message": f"选股失败: {e}", "picks": []}


def _render_homepage() -> str:
    status = _get_status()
    holdings = status["holdings"]
    signals = status.get("today_signals") or {}
    buy_signals = signals.get("buy_signals", [])
    sell_signals = signals.get("sell_signals", [])

    # 持仓行
    holding_rows = ""
    for code, h in holdings.items():
        pnl_cls = "profit" if h["pnl"] >= 0 else "loss"
        holding_rows += f"""
        <tr>
            <td>{code}</td>
            <td>{h['shares']}</td>
            <td>{h['avg_cost']:.3f}</td>
            <td>{h['current_price'] or '-'}</td>
            <td class="{pnl_cls}">{h['pnl']:+,.0f} ({h['pnl_pct']:+.1f}%)</td>
        </tr>"""

    # 今日建议 - 卖出
    sell_checks = ""
    for s in sell_signals:
        sell_checks += f"""
        <label><input type="checkbox" data-action="sell" data-code="{s['code']}" data-price="0"> 
        卖出 {s['code']} ({s.get('reason', '')})</label><br>"""

    # 今日建议 - 买入
    buy_checks = ""
    for b in buy_signals:
        price_val = b.get('price', 0) or 0
        shares_val = b.get('shares', 0) or 0
        buy_checks += f"""
        <label><input type="checkbox" data-action="buy" data-code="{b['code']}" data-shares="{shares_val}" data-price="{price_val}"> 
        买入 {b['code']} {shares_val}股@{price_val:.2f} ({b.get('reason', '')})</label><br>"""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>持仓同步</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 16px; background: #f5f5f5; color: #333; }}
h2 {{ margin: 20px 0 8px; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; margin: 8px 0; }}
th, td {{ padding: 8px; border: 1px solid #ddd; text-align: center; font-size: 14px; }}
th {{ background: #f0f0f0; }}
.summary {{ background: #fff; padding: 12px; border-radius: 8px; margin: 8px 0; }}
.profit {{ color: #c0392b; }}
.loss {{ color: #27ae60; }}
button {{ background: #2980b9; color: #fff; border: none; padding: 10px 20px; border-radius: 6px; font-size: 16px; cursor: pointer; margin: 4px; }}
button:active {{ background: #2471a3; }}
input, select {{ padding: 8px; margin: 4px; font-size: 14px; border: 1px solid #ddd; border-radius: 4px; width: 80px; }}
.section {{ background: #fff; padding: 12px; border-radius: 8px; margin: 8px 0; }}
#result {{ margin: 8px 0; padding: 12px; border-radius: 8px; display: none; }}
.ok {{ background: #d5f5e3; }}
.err {{ background: #fadbd8; }}
</style>
</head>
<body>
<h2>📊 持仓概览</h2>
<div class="summary">
    <b>现金:</b> {status['cash']:,.0f} 元 &nbsp; 
    <b>总资产:</b> {status['total_value']:,.0f} 元 &nbsp;
    <b>盈亏:</b> <span class="{'profit' if status['total_pnl'] >= 0 else 'loss'}">{status['total_pnl']:+,.0f} ({status['total_pnl_pct']:+.1f}%)</span>
</div>

<table>
<tr><th>代码</th><th>股数</th><th>成本</th><th>现价</th><th>盈亏</th></tr>
{holding_rows or '<tr><td colspan="5">空仓</td></tr>'}
</table>

<h2>📋 今日建议</h2>
<div class="section">
{''.join(sell_checks) or '无卖出建议'}<br>
{''.join(buy_checks) or '无买入建议'}
</div>

<h2>✏️ 手动操作</h2>
<div class="section">
代码: <input id="m_code" placeholder="000001">
方向: <select id="m_action"><option value="buy">买入</option><option value="sell">卖出</option></select>
股数: <input id="m_shares" placeholder="100" type="number">
价格: <input id="m_price" placeholder="10.50" type="number" step="0.01">
</div>

<button onclick="doSync()">确认同步</button>
<button onclick="location.reload()">刷新</button>

<div id="result"></div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';

async function doSync() {{
    const buys = [];
    const sells = [];

    // 勾选的建议
    document.querySelectorAll('input[type=checkbox]:checked').forEach(cb => {{
        if (cb.dataset.action === 'buy') {{
            buys.push({{code: cb.dataset.code, shares: parseInt(cb.dataset.shares), price: parseFloat(cb.dataset.price)}});
        }} else {{
            sells.push({{code: cb.dataset.code, price: parseFloat(cb.dataset.price)}});
        }}
    }});

    // 手动输入
    const mCode = document.getElementById('m_code').value.trim();
    const mAction = document.getElementById('m_action').value;
    const mShares = parseInt(document.getElementById('m_shares').value);
    const mPrice = parseFloat(document.getElementById('m_price').value);
    if (mCode && !isNaN(mShares) && !isNaN(mPrice)) {{
        if (mAction === 'buy') buys.push({{code: mCode, shares: mShares, price: mPrice}});
        else sells.push({{code: mCode, price: mPrice}});
    }}

    if (buys.length === 0 && sells.length === 0) {{
        alert('请勾选建议或输入手动操作');
        return;
    }}

    try {{
        const resp = await fetch('/api/sync?token=' + TOKEN, {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{executed_buys: buys, executed_sells: sells}})
        }});
        const data = await resp.json();
        const el = document.getElementById('result');
        el.style.display = 'block';
        if (data.success) {{
            el.className = 'ok';
            el.innerHTML = '✓ ' + data.results.join('<br>');
            if (data.warning) el.innerHTML += '<br>⚠ ' + data.warning;
            setTimeout(() => location.reload(), 1500);
        }} else {{
            el.className = 'err';
            el.innerHTML = '✗ ' + data.errors.join('<br>');
        }}
    }} catch(e) {{
        alert('请求失败: ' + e);
    }}
}}
</script>
</body>
</html>"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    import uvicorn
    print(f"启动持仓同步服务: http://0.0.0.0:{args.port}/?token={WEB_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
