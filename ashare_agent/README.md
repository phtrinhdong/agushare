# A股K线形态扫描 Agent

每个交易日自动扫描 A 股全市场，识别经典看涨形态 + 技术指标信号，输出 Excel 报告 + K线图 + 邮件推送。

## 功能

**K线形态**
- 单K：锤子线 / 倒锤子 / 底部十字星
- 双K：看涨吞没 / 曙光初现(刺透)
- 三K：启明星 / 红三兵
- 多K：上升三法 / 向上跳空缺口 / 均线多头排列 / 放量突破MA20

**技术指标**
- MACD 低位金叉
- KDJ 超卖金叉
- RSI 超卖反弹
- 布林带下轨反弹

**输出**
- 控制台彩色表格
- Excel 报告（按综合得分排序）
- 命中股票的 K线图（带均线、成交量、买点箭头、信号注释）
- 可选邮件推送（SMTP）

## 安装

```bash
pip install -r requirements.txt
```

## 使用

```bash
# 配置: 编辑 config.yaml（数据源、形态开关、邮件、调度时间...）

# 1. 单次扫描全市场
python -m ashare_agent.main run

# 2. 只扫描自选股
python -m ashare_agent.main run --watchlist

# 3. 检查单只股票当前信号
python -m ashare_agent.main inspect 600519

# 4. 启动定时调度（每交易日 15:35 自动扫描）
python -m ashare_agent.main schedule

# 5. 启动 Web 看板 (http://127.0.0.1:8000)
python -m ashare_agent.main serve
```

## Web 看板

打开浏览器访问 http://127.0.0.1:8000

- **信号列表**：可按代码/名称/收盘价/涨跌幅/得分点击表头排序
- **筛选**：按代码或名称搜索，按形态过滤，按最低得分过滤
- **触发扫描** / **扫描自选股**：右上角按钮一键触发，状态栏实时显示进度
- **查看K线**：点击表格右侧链接，弹出该股票的K线图

API 端点：
- `GET  /api/signals`        最新扫描结果
- `GET  /api/patterns`       所有已注册的形态/指标
- `GET  /api/scan/status`    扫描进度
- `POST /api/scan`           触发扫描 (?watchlist_only=true 仅自选股)
- `GET  /api/chart/{code}`   返回 K 线图 PNG
- `GET  /api/snapshots`      历史扫描列表

## 自定义形态 (插件式)

在 `patterns/custom/` 目录下新建任意 `.py` 文件，加 `@register_pattern` 即可，无需改主代码。

```python
# ashare_agent/patterns/custom/my_pattern.py
from ashare_agent.patterns.registry import register_pattern

@register_pattern(name="my_rocket", desc="火箭发射形态", score=4)
def detect(df):
    # 返回 True 表示命中
    return df["close"].iloc[-1] > df["close"].iloc[-5] * 1.1
```

重启后程序会自动加载，Web 页面、Excel 报告、邮件、控制台都会显示新形态命中的结果。

`patterns/custom/example_volume_surge.py` 包含了完整示例（包括自定义技术指标 `@register_indicator`）。

## 配置要点

- `config.yaml` 的 `patterns.*` 节可逐个开关每个形态/指标
- `filter.min_score` 控制最低综合得分（默认 3 分起）
- `email.enabled: true` 开启邮件推送，需要填 SMTP 授权码
- `schedule.cron_hour / cron_minute` 设置每日扫描时间

## 项目结构

```
ashare_agent/
├── main.py            # CLI 入口 (run / schedule / inspect)
├── scanner.py         # 并发扫描器 + 评分
├── data_loader.py     # akshare 数据获取 + 本地缓存
├── patterns/
│   ├── candlestick.py # K线形态识别
│   └── indicators.py  # MACD / KDJ / RSI / BOLL
├── reporter.py        # Excel + 邮件
├── visualizer.py      # mplfinance K线图
├── utils.py           # 配置 / 日志 / 交易日
├── config.yaml        # 全部可调参数
└── output/
    ├── reports/       # Excel 报告
    └── charts/        # K线图 PNG
```

## 免责声明

本工具识别的形态仅为统计学倾向，不构成投资建议。实盘操作请结合基本面、行业研究和风险管理。市场有风险，投资需谨慎。
