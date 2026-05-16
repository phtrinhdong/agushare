"""
报告生成 (CSV + Excel) 与邮件推送
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path

import pandas as pd

from .scanner import SignalRow
from .utils import ensure_dir

log = logging.getLogger("ashare_agent")


# ============================================================
# 控制台
# ============================================================
def print_console(rows: list[SignalRow]) -> None:
    if not rows:
        print("\n[今日无符合条件的股票]")
        return
    print(f"\n========== 命中买入信号 ({len(rows)} 只) ==========")
    print(f"{'代码':<8}{'名称':<10}{'收盘':>8}{'涨跌%':>8}{'得分':>6}  形态/指标")
    print("-" * 90)
    for r in rows:
        sig = " | ".join(filter(None, [
            ",".join(r.candlestick),
            ",".join(r.indicators),
        ]))
        name = (r.name[:6] + "..") if len(r.name) > 8 else r.name
        print(f"{r.code:<8}{name:<10}{r.close:>8.2f}{r.chg_pct:>8.2f}{r.score:>6}  {sig}")
    print()


# ============================================================
# Excel 报告
# ============================================================
def save_excel(rows: list[SignalRow], out_dir: str | Path) -> Path:
    out_dir = ensure_dir(out_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    fp = Path(out_dir) / f"signals_{stamp}.xlsx"

    if not rows:
        # 仍输出空表
        df = pd.DataFrame(columns=["代码", "名称", "收盘价", "涨跌幅%", "综合得分", "K线形态", "技术指标"])
    else:
        df = pd.DataFrame([r.to_dict() for r in rows])

    with pd.ExcelWriter(fp, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="买入信号", index=False)
        # 调整列宽
        ws = writer.sheets["买入信号"]
        widths = {"A": 10, "B": 14, "C": 10, "D": 10, "E": 10, "F": 35, "G": 35}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w

    log.info("Excel 报告: %s", fp)
    return fp


def save_csv(rows: list[SignalRow], out_dir: str | Path) -> Path:
    out_dir = ensure_dir(out_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    fp = Path(out_dir) / f"signals_{stamp}.csv"
    df = pd.DataFrame([r.to_dict() for r in rows])
    df.to_csv(fp, index=False, encoding="utf-8-sig")
    return fp


# ============================================================
# 邮件
# ============================================================
def send_email(
    cfg: dict,
    rows: list[SignalRow],
    attachments: list[Path] | None = None,
) -> None:
    if not cfg.get("enabled"):
        log.info("邮件推送未启用,跳过")
        return

    sender = cfg["sender"]
    password = cfg["password"]
    receivers = cfg["receivers"]
    smtp_server = cfg["smtp_server"]
    smtp_port = int(cfg["smtp_port"])
    use_ssl = bool(cfg.get("use_ssl", True))
    subject_prefix = cfg.get("subject_prefix", "[A股形态扫描]")

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = f"{subject_prefix} {stamp} 命中 {len(rows)} 只"

    # HTML 邮件
    if rows:
        df = pd.DataFrame([r.to_dict() for r in rows])
        html_table = df.to_html(index=False, escape=False,
                                border=1, justify="center")
    else:
        html_table = "<p>今日无符合条件的股票。</p>"

    html = f"""
    <html><body>
    <h3>A股K线形态扫描结果 ({stamp})</h3>
    <p>共命中 <b>{len(rows)}</b> 只股票（按综合得分排序）。</p>
    {html_table}
    <p style="color:#888;font-size:12px">本邮件由 AGUagent 自动生成,仅供研究参考,不构成投资建议。</p>
    </body></html>
    """

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(receivers)
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html", "utf-8"))

    # 附件
    for fp in attachments or []:
        if not Path(fp).exists():
            continue
        with open(fp, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        f'attachment; filename="{Path(fp).name}"')
        msg.attach(part)

    try:
        if use_ssl:
            srv = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
        else:
            srv = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            srv.starttls()
        srv.login(sender, password)
        srv.sendmail(sender, receivers, msg.as_string())
        srv.quit()
        log.info("邮件已发送到 %s", receivers)
    except Exception as e:
        log.error("邮件发送失败: %s", e)
