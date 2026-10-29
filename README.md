# A股量化交易策略集 (Quantitative Trading Strategies for A-Shares)

> 面向某券商 **QMT（Quantitative Trading）策略交易端** 的内置 Python 框架实现。
> 源码原始编码为 **GBK**，本仓库已统一转码为 **UTF-8**，中文注释可直接阅读。

## 📁 项目结构

```
├── dxgp/           涨停缩量低吸策略（某券商QMT策略交易端 Python框架版）
│   ├── zt_shrink_strategy_gbk.py   # 策略主源码 (V2.6.189)
│   ├── download_trading_calendar.py
│   ├── set_daily_buys.py
│   ├── health_check_qmt.py
│   ├── daily_task_runner.py
│   └── V2615x_变更报告.md
│
└── nbbank/         银行股 QMT 原生多策略
    ├── gf_qmt_native_multi_gbk.py  # 策略主源码 (v1.2.95)
    └── backtest_optimize.py
```

## 🎯 策略简介

### dxgp — 涨停缩量低吸
- 面向 A 股涨停板次日缩量低吸场景
- 基于某券商 QMT 内置 Python 框架，含实时行情订阅、交易回报处理、状态持久化
- 内置账号隔离：状态/账本/黑名单/待办文件名自动带资金账号后缀（多账号/多实例不串号）

### nbbank — 银行股多策略
- 面向银行股的低波动/高股息交易
- 原生多策略框架，含回测对齐、情绪指标、仓位管理

## 🔒 隐私说明

发布前已做如下脱敏处理（**原始工作目录未参与发布，仅导出发布副本**）：

| 项 | 处理 |
|---|---|
| 资金账号 | `DEFAULT_ACCT = "<你的资金账号>"` → 已脱敏为占位符 |
| 交易日志 / 盈亏历史 / 运行状态 | 未包含 |
| 临时调试脚本（`_*.py`） | 未包含 |
| 备份文件（`.bak*`） | 未包含 |
| Python 解释器绝对路径 | 已替换为占位符 |
| 编码 | GBK → UTF-8 转码 |

## ⚠️ 免责声明

本仓库仅作**技术学习与交流**。量化交易策略存在风险，历史回测结果不代表未来收益。
请勿直接用于实盘交易，如有需求请自行评估风险并遵守相关法律法规。

## 🛠 运行环境

- Python 3.11+
- 某券商 QMT 策略交易终端
- 依赖库见各源码文件头部的 import