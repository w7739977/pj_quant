# Skills 配置记录

**记录日期**: 2026-05-09
**用途**: 同步到其他环境

## Skills 目录结构

```
pj_quant/.claude/skills/
├── quant-analyst/SKILL.md   # 量化分析师
├── python-pro/SKILL.md      # Python 专家
├── ml-engineer/SKILL.md     # ML 工程师
└── superpowers/SKILL.md     # Skills 调度规则
```

## 各 Skill 说明

### 1. quant-analyst
量化分析师，专注金融建模、算法交易、风险分析。
- 触发场景: 交易策略开发、回测、风险模型、组合优化
- 可用工具: Read, Write, Edit, Bash, Glob, Grep
- 覆盖: 定价模型、波动率建模、统计套利、动量/均值回归、VaR、压力测试

### 2. python-pro
Python 3.11+ 专家，专注类型安全、异步、生产级代码。
- 触发场景: Python 开发、类型注解、pytest、async/await、dataclasses
- 角色: specialist
- 要求: 全部函数签名加 type hints, 90%+ 测试覆盖, mypy strict 通过
- 规范: 用 `X | None` 不用 `Optional[X]`, dataclasses 优于手写 `__init__`

### 3. ml-engineer
ML 工程师，专注生产 ML 系统全生命周期。
- 触发场景: 模型训练管线、模型服务、性能优化、自动重训练
- 模型: sonnet
- 覆盖: 特征工程、超参优化、蓝绿部署、模型监控、漂移检测

### 4. superpowers
Skills 调度元规则，定义何时、如何调用其他 skills。
- 核心规则: 有 1% 可能相关的 skill 就必须先调用
- 优先级: 用户指令 > superpowers > 系统默认
- 调用顺序: 流程类 skill 先于实现类 skill

## 同步到其他环境

```bash
# 将 .claude/skills/ 目录复制到目标环境
scp -r pj_quant/.claude/skills/ target:pj_quant/.claude/skills/
```

或直接复制各 SKILL.md 文件内容（见本目录下各子目录）。
