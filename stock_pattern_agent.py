"""
A股 K线形态监控 Agent
依赖: pip install akshare pandas numpy TA-Lib apscheduler requests
TA-Lib 安装较麻烦，Linux: apt install ta-lib;  Mac: brew install ta-lib
"""

import akshare as ak
import pandas as pd
import numpy as np
import talib
import logging
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)


# ============ 1. 数据层 ============

def get_stock_list():
    """获取A股全部代码,过滤掉ST、退市、北交所"""
    df = ak.stock_zh_a_spot_em()
    df = df[~df['名称'].str.contains('ST|退', na=False)]
    df = df[~df['代码'].str.startswith(('8', '4'))]  # 过滤北交所
    return df[['代码', '名称']].to_dict('records')


def get_kline(code, days=120):
    """拉单只股票的日线,前复权"""
    try:
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=(pd.Timestamp.now() - pd.Timedelta(days=days*2)).strftime('%Y%m%d'),
            end_date=pd.Timestamp.now().strftime('%Y%m%d'),
            adjust="qfq"
        )
        if df.empty or len(df) < 30:
            return None
        df = df.rename(columns={
            '日期': 'date', '开盘': 'open', '收盘': 'close',
            '最高': 'high', '最低': 'low', '成交量': 'volume'
        })
        return df.tail(days).reset_index(drop=True)
    except Exception as e:
        log.debug(f"{code} 拉取失败: {e}")
        return None


# ============ 2. 计算层:形态识别 ============

class PatternDetector:
    """形态识别集合,每个方法返回 (是否触发, 描述)"""

    @staticmethod
    def hammer_at_support(df):
        """锤子线 + 处在下跌趋势末端 + 5日线之下"""
        if len(df) < 20:
            return False, None
        hammer = talib.CDLHAMMER(df['open'], df['high'], df['low'], df['close'])
        if hammer.iloc[-1] == 0:
            return False, None
        # 过滤:必须前期下跌 (10日跌幅 > 5%)
        if df['close'].iloc[-1] / df['close'].iloc[-10] > 0.95:
            return False, None
        return True, "锤子线@下跌末端"

    @staticmethod
    def volume_breakout(df):
        """放量突破20日新高"""
        if len(df) < 25:
            return False, None
        high20 = df['high'].iloc[-21:-1].max()
        vol_ma5 = df['volume'].iloc[-6:-1].mean()
        last = df.iloc[-1]
        if last['close'] > high20 and last['volume'] > vol_ma5 * 1.8:
            return True, f"放量破20日新高(量比{last['volume']/vol_ma5:.1f})"
        return False, None

    @staticmethod
    def macd_golden_cross(df):
        """MACD零轴下方金叉 + 收盘站上20日线"""
        if len(df) < 35:
            return False, None
        macd, signal, _ = talib.MACD(df['close'])
        ma20 = df['close'].rolling(20).mean()
        # 昨日 DIF < signal, 今日 DIF > signal, 且 DIF < 0
        if (macd.iloc[-2] < signal.iloc[-2] and
            macd.iloc[-1] > signal.iloc[-1] and
            macd.iloc[-1] < 0 and
            df['close'].iloc[-1] > ma20.iloc[-1]):
            return True, "MACD零下金叉+破MA20"
        return False, None

    @staticmethod
    def engulfing_bullish(df):
        """看涨吞没"""
        if len(df) < 15:
            return False, None
        eng = talib.CDLENGULFING(df['open'], df['high'], df['low'], df['close'])
        if eng.iloc[-1] > 0:
            # 过滤:出现位置不能在高位 (距60日高点 > 10%)
            high60 = df['high'].tail(60).max()
            if df['close'].iloc[-1] / high60 < 0.92:
                return True, "看涨吞没@相对低位"
        return False, None

    @classmethod
    def scan_all(cls, df):
        """跑所有形态,返回所有命中"""
        hits = []
        for name in ['hammer_at_support', 'volume_breakout',
                     'macd_golden_cross', 'engulfing_bullish']:
            ok, desc = getattr(cls, name)(df)
            if ok:
                hits.append(desc)
        return hits


# ============ 3. 调度层 ============

def scan_one(stock):
    """处理单只股票,返回信号 dict 或 None"""
    df = get_kline(stock['代码'])
    if df is None:
        return None
    hits = PatternDetector.scan_all(df)
    if hits:
        return {
            'code': stock['代码'],
            'name': stock['名称'],
            'close': float(df['close'].iloc[-1]),
            'patterns': hits,
            'time': datetime.now().strftime('%Y-%m-%d %H:%M')
        }
    return None


def full_market_scan(max_workers=20):
    """全市场扫描"""
    log.info("开始全市场扫描...")
    stocks = get_stock_list()
    log.info(f"待扫描股票数: {len(stocks)}")

    signals = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(scan_one, s): s for s in stocks}
        for i, fut in enumerate(as_completed(futures)):
            if i % 200 == 0:
                log.info(f"进度 {i}/{len(stocks)}")
            result = fut.result()
            if result:
                signals.append(result)
            time.sleep(0.05)  # 限流,避免被ban

    log.info(f"扫描完成,命中 {len(signals)} 只")
    return signals


# ============ 4. 通知层 ============

class Notifier:
    """支持企业微信机器人 / Telegram / 自建TSDD服务器"""

    def __init__(self, config):
        self.config = config

    def send_wecom(self, text):
        """企业微信群机器人"""
        url = self.config.get('wecom_webhook')
        if not url:
            return
        requests.post(url, json={
            "msgtype": "markdown",
            "markdown": {"content": text}
        }, timeout=5)

    def send_telegram(self, text):
        token = self.config.get('tg_token')
        chat_id = self.config.get('tg_chat_id')
        if not token:
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=5
        )

    def push(self, signals):
        if not signals:
            log.info("无信号,不推送")
            return

        lines = [f"## A股形态扫描 {datetime.now():%Y-%m-%d %H:%M}",
                 f"命中 **{len(signals)}** 只:\n"]
        for s in signals[:30]:  # 限制单次推送数量
            patterns = " / ".join(s['patterns'])
            lines.append(f"- `{s['code']}` {s['name']} ¥{s['close']:.2f} — {patterns}")
        text = "\n".join(lines)

        self.send_wecom(text)
        self.send_telegram(text)
        # 同时落盘留档
        with open(f"signals_{datetime.now():%Y%m%d}.json", 'w', encoding='utf-8') as f:
            json.dump(signals, f, ensure_ascii=False, indent=2)


# ============ 5. 主入口 ============

def job():
    """每日收盘后执行"""
    try:
        signals = full_market_scan(max_workers=20)
        notifier = Notifier({
            'wecom_webhook': 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY',
            # 'tg_token': 'xxx', 'tg_chat_id': 'xxx',
        })
        notifier.push(signals)
    except Exception as e:
        log.exception(f"任务失败: {e}")


if __name__ == '__main__':
    # 立即跑一次测试
    job()

    # 定时调度: 每个交易日 15:30 执行
    scheduler = BlockingScheduler(timezone='Asia/Shanghai')
    scheduler.add_job(job, 'cron', day_of_week='mon-fri', hour=15, minute=30)
    log.info("调度器启动, 每交易日 15:30 自动扫描")
    scheduler.start()
