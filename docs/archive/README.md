# 历史文档归档（archive）

本目录存放已实施完成或已废弃的修复 prompt / 设计文档。

## 已实施完成

| 文档 | 主题 | 实施 commit |
|------|------|-------------|
| `FIX_PROMPT_BAOSTOCK_REMOVAL.md` | 移除 BaoStock 统一 Tushare | 66b47bb / 6df6a85 / fb04b85 |
| `FIX_PROMPT_EVOLVE_CLEANUP.md` | auto_evolve 重构尾巴清理 | 71b420e |
| `FIX_PROMPT_REASON_REFACTOR.md` | humanize_reason 结构化 | 836718c / f5fba34 |
| `FIX_PROMPT_NEUTRALIZATION_8DIMS.md` | 中性化 + 8 维度 + 推荐 10 | 17fb181 / 6c2cfff / 8b8fe57 / f1c8a62 |
| `FIX_PROMPT_NEUTRALIZE_PER_SECTION.md` | 中性化按截面分组（v6 验证后默认禁用）| 1607363 |
| `FIX_PROMPT_SENTIMENT_BC.md` | FinBERT + sentiment_history（B+C 代码就位）| 356535f / ed356e9 / 5f7a0f9 / 316e80a / 063ba73 / e28371e |
| `FIX_PROMPT_P0_FINANCIAL_FACTORS.md` | P0 财务因子（ROE/营收增速/EPS/负债率）| 6811d04 |

## 已废弃

| 文档 | 原因 |
|------|------|
| `OPTIMIZATION_SUMMARY.md` | 4 月初的本地优先架构总结，已被后续多轮迭代取代 |
| `CODE_REVIEW.md` | 4 月初的初版 review，已被后续 6 轮 review 覆盖 |

## 当前生产文档

根目录保留：
- `README.md` — 项目总览
- `PROGRESS.md` — 开发进度（含历次实验结论）
- `DEPLOY.md` — 部署指南

`docs/`：
- `strategy_explained.md` — 选股逻辑说明
- `eight_dimensions_plan.md` — 8 维度分析设计
- `optimization_backlog.md` — 优化待办（含失败方案的实证记录）

---

> 历史文档作为决策依据保留，**不应**作为新功能的实施基线。
> 新功能请基于当前生产代码 + PROGRESS.md 当前状态做评估。
