"""
图表生成器 — 根据数据形态自动选图
- matplotlib,中文字体 fallback
- 输出 PNG 到 reports/ 目录
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import os
from typing import Dict, Any, List
from datetime import datetime

# 字体 fallback,避免中文乱码
import matplotlib.font_manager as fm
import warnings
warnings.filterwarnings("ignore")

# 尝试找一个中文字体
CN_FONTS = ["Noto Sans CJK SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei",
            "SimHei", "Microsoft YaHei", "PingFang SC", "Heiti SC", "Arial Unicode MS"]
CHOSEN_FONT = None
for f in CN_FONTS:
    try:
        fm.findfont(f, fallback_to_default=False)
        CHOSEN_FONT = f
        break
    except Exception:
        continue
if CHOSEN_FONT:
    plt.rcParams["font.sans-serif"] = [CHOSEN_FONT]
    plt.rcParams["axes.unicode_minus"] = False

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


def _to_float(v):
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


def _to_str(v):
    return str(v) if v is not None else ""


def pick_chart_type(data: Dict[str, Any]) -> str:
    """根据数据形态选图"""
    cols = data.get("columns", [])
    rows = data.get("rows", [])
    if not rows or not cols:
        return "empty"

    n = len(rows)
    # 1 行 → KPI 数字卡
    if n == 1 and len(cols) == 1:
        return "kpi"

    # 找日期列 / 数值列 / 类目列
    date_cols = [c for c in cols if any(k in c.lower() for k in ["date", "time", "day", "month", "日期"])]
    num_cols = [c for c in cols if any(k in c.lower() for k in ["gmv", "amt", "amount", "cnt", "rate", "pct", "roi", "value", "数", "额", "率", "价"])]
    cat_cols = [c for c in cols if c not in date_cols and c not in num_cols]

    if date_cols and num_cols:
        return "line"  # 时间序列
    if num_cols and cat_cols:
        return "bar"   # 分类对比
    if len(num_cols) >= 2 and not cat_cols:
        return "line"  # 多指标时序
    return "bar"


def render(data: Dict[str, Any], title: str = "", output_name: str = None) -> str:
    """
    渲染图,返回 PNG 路径
    """
    if data.get("error") or not data.get("rows"):
        # 空数据也要画个"无数据"占位
        return _render_empty(title, output_name)

    chart_type = pick_chart_type(data)
    if output_name is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:17]
        output_name = f"chart_{ts}.png"
    out_path = os.path.join(REPORTS_DIR, output_name)

    cols = data["columns"]
    rows = data["rows"]

    if chart_type == "kpi":
        return _render_kpi(rows, cols, title, out_path)
    if chart_type == "line":
        return _render_line(rows, cols, title, out_path)
    if chart_type == "bar":
        return _render_bar(rows, cols, title, out_path)

    return _render_empty(title, output_name)


def _render_kpi(rows, cols, title, out_path):
    val = _to_float(rows[0][cols[0]])
    fig, ax = plt.subplots(figsize=(8, 4), dpi=100)
    ax.text(0.5, 0.55, f"{val:,.2f}", ha="center", va="center",
            fontsize=48, fontweight="bold", color="#1f77b4")
    ax.text(0.5, 0.15, cols[0], ha="center", va="center",
            fontsize=16, color="#666")
    if title:
        ax.set_title(title, fontsize=14, pad=20)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", dpi=100)
    plt.close(fig)
    return out_path


def _render_line(rows, cols, title, out_path):
    fig, ax = plt.subplots(figsize=(11, 5), dpi=100)

    # 找日期列(第一列含 date/time/day)
    date_col = next((c for c in cols if any(k in c.lower() for k in ["date", "time", "day", "month"])), cols[0])
    num_cols = [c for c in cols if c != date_col and any(k in c.lower() for k in ["gmv", "amt", "amount", "cnt", "rate", "pct", "roi", "value", "数", "额", "率", "价"])]
    if not num_cols:
        num_cols = [c for c in cols if c != date_col]

    x = [str(r[date_col]) for r in rows]
    for nc in num_cols[:5]:  # 最多画 5 条线
        y = [_to_float(r.get(nc)) for r in rows]
        ax.plot(x, y, marker="o", markersize=3, label=nc)
    ax.set_xlabel(date_col)
    ax.legend(loc="best", fontsize=9)
    if title:
        ax.set_title(title, fontsize=13)
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", dpi=100)
    plt.close(fig)
    return out_path


def _render_bar(rows, cols, title, out_path):
    fig, ax = plt.subplots(figsize=(11, max(4, len(rows) * 0.35)), dpi=100)

    # 找类目列和数值列
    cat_col = None
    for c in cols:
        if c not in [c2 for c2 in cols if any(k in c2.lower() for k in ["gmv", "amt", "amount", "cnt", "rate", "pct", "roi", "value", "数", "额", "率", "价"])]:
            cat_col = c
            break
    if not cat_col:
        cat_col = cols[0]

    num_col = None
    for c in cols:
        if c != cat_col and any(k in c.lower() for k in ["gmv", "amt", "amount", "cnt", "rate", "pct", "roi", "value", "数", "额", "率", "价"]):
            num_col = c
            break
    if not num_col:
        # 第一个非 cat_col
        for c in cols:
            if c != cat_col:
                num_col = c
                break

    # 按数值降序,top 20
    sorted_rows = sorted(rows, key=lambda r: _to_float(r.get(num_col)), reverse=True)[:20]

    labels = [_to_str(r[cat_col]) for r in sorted_rows]
    values = [_to_float(r.get(num_col)) for r in sorted_rows]

    # 横向条形,长类目名更好读
    y_pos = range(len(labels))
    colors = ["#2ca02c" if v > 0 else "#d62728" for v in values]
    ax.barh(y_pos, values, color=colors, alpha=0.8)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()  # 最大的在上面
    ax.set_xlabel(num_col)
    if title:
        ax.set_title(title, fontsize=13)
    ax.grid(True, axis="x", alpha=0.3)

    # 数值标签
    for i, v in enumerate(values):
        ax.text(v, i, f" {v:,.0f}", va="center", fontsize=8)

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", dpi=100)
    plt.close(fig)
    return out_path


def _render_empty(title, output_name):
    if output_name is None:
        output_name = f"chart_empty_{datetime.now().strftime('%H%M%S')}.png"
    out_path = os.path.join(REPORTS_DIR, output_name)
    fig, ax = plt.subplots(figsize=(8, 4), dpi=100)
    ax.text(0.5, 0.5, "暂无数据", ha="center", va="center", fontsize=24, color="#999")
    if title:
        ax.set_title(title, fontsize=14)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", dpi=100)
    plt.close(fig)
    return out_path
