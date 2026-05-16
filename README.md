# agushare

A股 K 线形态扫描 Agent —— 自动监控全市场，识别经典看涨形态 + 技术指标信号，输出 Excel 报告 / K 线图 / 邮件推送 / Web 看板。

## 功能一览

- **K线形态**：锤子线、倒锤子、底部十字星、看涨吞没、曙光初现(刺透)、启明星、红三兵、上升三法、向上跳空缺口、均线多头排列、放量突破 MA20
- **技术指标**：MACD 低位金叉、KDJ 超卖金叉、RSI 超卖反弹、布林带下轨反弹
- **插件式自定义形态**：`ashare_agent/patterns/custom/` 目录下放任意 `.py`，加 `@register_pattern` 即可自动注册
- **多种输出**：控制台表格、Excel 报告、带标注的 K线图 PNG、HTML 邮件、Web 看板
- **运行模式**：单次扫描 / 每个交易日 15:35 定时盘后扫描 / 单股检查 / Web 服务

## 快速开始

```bash
pip install -r ashare_agent/requirements.txt

python -m ashare_agent.main run              # 全市场扫一次
python -m ashare_agent.main inspect 600519   # 看单股当前信号
python -m ashare_agent.main schedule         # 每交易日 15:35 定时跑
python -m ashare_agent.main serve            # 启动 Web 看板 http://127.0.0.1:8000
```

完整使用文档与配置说明：[`ashare_agent/README.md`](ashare_agent/README.md)

## 项目结构

```
agushare/
├── ashare_agent/              # 主包
│   ├── main.py                # CLI 入口 (run / schedule / inspect / serve)
│   ├── server.py              # FastAPI Web 后端
│   ├── scanner.py             # 并发扫描器
│   ├── data_loader.py         # akshare 数据获取 + 本地缓存
│   ├── patterns/
│   │   ├── candlestick.py     # K线形态
│   │   ├── indicators.py      # 技术指标
│   │   ├── registry.py        # 插件注册中心
│   │   └── custom/            # 用户自定义形态目录
│   ├── reporter.py            # Excel + 邮件
│   ├── visualizer.py          # mplfinance K线图
│   ├── storage.py             # 扫描快照持久化
│   ├── web/index.html         # 前端单页
│   ├── config.yaml            # 全部可调参数
│   └── README.md              # 详细文档
└── README.md
```

## 免责声明

本工具识别的形态仅为统计学倾向，不构成任何投资建议。市场有风险，投资需谨慎。
