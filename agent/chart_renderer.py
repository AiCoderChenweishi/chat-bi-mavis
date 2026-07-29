"""
图表生成器 — 根据数据形态自动选图
- ECharts option JSON (主路径,前端渲染)
- matplotlib PNG (兼容旧路径,可选)
- 输出 ECharts: reports/echarts/<sid>.json
"""
import os
import json
import warnings
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

warnings.filterwarnings("ignore")

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")
ECHARTS_DIR = os.path.join(REPORTS_DIR, "echarts")
os.makedirs(REPORTS_DIR, exist_ok=True)
os.makedirs(ECHARTS_DIR, exist_ok=True)


def _to_float(v):
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


def _to_str(v):
    return str(v) if v is not None else ""


# ============ 主入口: ECharts ============

def pick_chart_type(data: Dict[str, Any]) -> str:
    """根据数据形态选图(共用,kpi/line/bar/pie)"""
    cols = data.get("columns", [])
    rows = data.get("rows", [])
    if not rows or not cols:
        return "empty"

    n = len(rows)
    if n == 1 and len(cols) == 1:
        return "kpi"

    date_cols = [c for c in cols if any(k in c.lower() for k in ["date", "time", "day", "month", "日期"])]
    num_cols = [c for c in cols if any(k in c.lower() for k in ["gmv", "amt", "amount", "cnt", "rate", "pct", "roi", "value", "数", "额", "率", "价"])]
    cat_cols = [c for c in cols if c not in date_cols and c not in num_cols]

    if date_cols and num_cols:
        return "line"
    if num_cols and cat_cols:
        return "bar"
    if len(num_cols) >= 2 and not cat_cols:
        return "line"
    return "bar"


def _classify_cols(cols: List[str], rows: Optional[List[Dict]] = None) -> Tuple[List[str], List[str], List[str]]:
    """返回 (date_cols, num_cols, cat_cols)。
    启发式: 名称关键词 + 数据类型双重判断
    """
    date_kw = ["date", "time", "day", "month", "year", "日期", "时间", "年月"]
    num_kw = ["gmv", "amt", "amount", "cnt", "count", "rate", "pct", "roi", "value",
              "sum", "avg", "min", "max", "total", "rev", "revenue", "price",
              "数", "额", "率", "价", "值", "比"]

    date_cols = [c for c in cols if any(k in c.lower() for k in date_kw)]
    candidate_num = [c for c in cols if c not in date_cols and any(k in c.lower() for k in num_kw)]

    # 如果有数据,用值类型二次验证 — 非名称匹配但所有值都是数字的列也算 num
    if rows:
        for c in cols:
            if c in date_cols or c in candidate_num:
                continue
            sample = [r.get(c) for r in rows[:5] if r.get(c) is not None]
            if sample and all(isinstance(v, (int, float)) or _to_float(v) != 0.0 for v in sample):
                # 但如果值都是字符串(category), 也不算 num
                if all(isinstance(v, (int, float)) for v in sample):
                    candidate_num.append(c)

    cat_cols = [c for c in cols if c not in date_cols and c not in candidate_num]
    return date_cols, candidate_num, cat_cols


def _build_option_kpi(rows, cols, title: str) -> Dict:
    val = _to_float(rows[0][cols[0]])
    label = cols[0]
    return {
        "title": {"text": title or label, "left": "center", "textStyle": {"fontSize": 14, "color": "#1f2937"}},
        "tooltip": {"trigger": "item", "formatter": f"{label}: <b>{val:,.2f}</b>"},
        "series": [{
            "type": "gauge" if False else "pie",  # 用 pie 显示数字
            "radius": ["55%", "70%"],
            "startAngle": 90,
            "endAngle": -270,
            "animation": False,
            "data": [{"value": val, "name": label}],
            "label": {
                "show": True,
                "position": "center",
                "formatter": f"{{c|{val:,.2f}}}\n{{b|{label}}}",
                "rich": {
                    "c": {"fontSize": 40, "fontWeight": "bold", "color": "#2563eb"},
                    "b": {"fontSize": 14, "color": "#6b7280", "padding": [10, 0, 0, 0]},
                },
            },
            "itemStyle": {"color": "#2563eb"},
        }],
    }


def _build_option_line(rows, cols, title: str) -> Dict:
    date_cols, num_cols, _ = _classify_cols(cols, rows)
    date_col = date_cols[0] if date_cols else cols[0]
    if not num_cols:
        num_cols = [c for c in cols if c != date_col]

    x = [_to_str(r.get(date_col)) for r in rows]
    series = []
    colors = ["#2563eb", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4"]
    for i, nc in enumerate(num_cols[:7]):  # 最多 7 条线
        y = [_to_float(r.get(nc)) for r in rows]
        series.append({
            "name": nc,
            "type": "line",
            "smooth": True,
            "symbol": "circle",
            "symbolSize": 6,
            "lineStyle": {"width": 2.5, "color": colors[i % len(colors)]},
            "itemStyle": {"color": colors[i % len(colors)]},
            "data": y,
            "emphasis": {"focus": "series"},
        })
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14, "color": "#1f2937"}},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"}},
        "legend": {"bottom": 5, "type": "scroll", "textStyle": {"fontSize": 11}},
        "grid": {"left": "5%", "right": "5%", "top": "13%", "bottom": "13%", "containLabel": True},
        "xAxis": {"type": "category", "data": x, "axisLabel": {"interval": "auto", "rotate": 0, "fontSize": 10}},
        "yAxis": {"type": "value", "axisLabel": {"fontSize": 10}},
        "series": series,
        "dataZoom": [{"type": "inside"}, {"type": "slider", "height": 18, "bottom": 30}] if len(rows) > 12 else None,
    }


def _build_option_bar(rows, cols, title: str) -> Dict:
    _, num_cols, cat_cols = _classify_cols(cols, rows)
    cat_col = cat_cols[0] if cat_cols else cols[0]
    if not num_cols:
        num_cols = [c for c in cols if c != cat_col]
    num_col = num_cols[0]

    # 排序 + top 20
    sorted_rows = sorted(rows, key=lambda r: _to_float(r.get(num_col)), reverse=True)[:20]
    labels = [_to_str(r.get(cat_col)) for r in sorted_rows]
    values = [_to_float(r.get(num_col)) for r in sorted_rows]

    is_negative = any(v < 0 for v in values)
    bar_data = [
        {
            "value": v,
            "itemStyle": {"color": "#ef4444" if v < 0 else ("#10b981" if v > 0 and is_negative else "#2563eb")},
        }
        for v in values
    ]
    return {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14, "color": "#1f2937"}},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}, "formatter": "{b}: <b>{c}</b>"},
        "grid": {"left": "3%", "right": "8%", "top": "13%", "bottom": "5%", "containLabel": True},
        "xAxis": {"type": "value", "axisLabel": {"fontSize": 10}},
        "yAxis": {"type": "category", "data": labels, "axisLabel": {"fontSize": 11}, "inverse": True},
        "series": [{
            "name": num_col,
            "type": "bar",
            "data": bar_data,
            "label": {"show": True, "position": "right", "formatter": "{c}", "fontSize": 10, "color": "#1f2937"},
            "barMaxWidth": 28,
        }],
    }


def _build_option_empty(title: str) -> Dict:
    return {
        "title": {"text": title or "暂无数据", "left": "center", "textStyle": {"fontSize": 14, "color": "#9ca3af"}},
        "graphic": [{
            "type": "text",
            "left": "center", "top": "middle",
            "style": {"text": "暂无数据", "fontSize": 18, "fill": "#9ca3af"},
        }],
    }


def build_echart_option(data: Dict[str, Any], title: str = "") -> Dict:
    """
    主入口: 接受 sql_result 字典,返 ECharts option dict
    空数据 → 返 '暂无数据' 占位
    """
    if data.get("error") or not data.get("rows") or not data.get("columns"):
        return _build_option_empty(title)

    chart_type = pick_chart_type(data)
    cols = data["columns"]
    rows = data["rows"]

    if chart_type == "kpi":
        return _build_option_kpi(rows, cols, title)
    if chart_type == "line":
        return _build_option_line(rows, cols, title)
    if chart_type == "bar":
        return _build_option_bar(rows, cols, title)
    return _build_option_empty(title)


def render_echart(data: Dict[str, Any], title: str = "", session_id: str = "") -> str:
    """
    渲染 ECharts option,写到 reports/echarts/<session_id>.json
    返回 url 路径: /api/echart/<session_id>
    """
    if not session_id:
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:17]
    option = build_echart_option(data, title)
    out_path = os.path.join(ECHARTS_DIR, f"{session_id}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(option, f, ensure_ascii=False, indent=2)
    return f"/api/echart/{session_id}"


# ============ 兼容: matplotlib PNG (老路径, 默认不再用) ============

def render(data: Dict[str, Any], title: str = "", output_name: str = None) -> str:
    """
    旧入口,画 PNG。保留向后兼容。
    新代码请用 render_echart()
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm
    except ImportError:
        return ""

    CN_FONTS = ["Noto Sans CJK SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei",
                "SimHei", "Microsoft YaHei", "PingFang SC", "Heiti SC", "Arial Unicode MS"]
    for f in CN_FONTS:
        try:
            fm.findfont(f, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [f]
            plt.rcParams["axes.unicode_minus"] = False
            break
        except Exception:
            continue

    if data.get("error") or not data.get("rows"):
        return _render_png_empty(plt, title, output_name)

    chart_type = pick_chart_type(data)
    if output_name is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:17]
        output_name = f"chart_{ts}.png"
    out_path = os.path.join(REPORTS_DIR, output_name)
    cols = data["columns"]
    rows = data["rows"]

    if chart_type == "kpi":
        return _render_png_kpi(plt, rows, cols, title, out_path)
    if chart_type == "line":
        return _render_png_line(plt, rows, cols, title, out_path)
    if chart_type == "bar":
        return _render_png_bar(plt, rows, cols, title, out_path)
    return _render_png_empty(plt, title, output_name)


def _render_png_kpi(plt, rows, cols, title, out_path):
    val = _to_float(rows[0][cols[0]])
    fig, ax = plt.subplots(figsize=(8, 4), dpi=100)
    ax.text(0.5, 0.55, f"{val:,.2f}", ha="center", va="center",
            fontsize=48, fontweight="bold", color="#1f77b4")
    ax.text(0.5, 0.15, cols[0], ha="center", va="center", fontsize=16, color="#666")
    if title:
        ax.set_title(title, fontsize=14, pad=20)
    ax.axis("off")
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", dpi=100)
    plt.close(fig)
    return out_path


def _render_png_line(plt, rows, cols, title, out_path):
    date_col = next((c for c in cols if any(k in c.lower() for k in ["date", "time", "day", "month"])), cols[0])
    num_cols = [c for c in cols if c != date_col and any(k in c.lower() for k in ["gmv", "amt", "amount", "cnt", "rate", "pct", "roi", "value", "数", "额", "率", "价"])]
    if not num_cols:
        num_cols = [c for c in cols if c != date_col]
    fig, ax = plt.subplots(figsize=(11, 5), dpi=100)
    x = [_to_str(r.get(date_col)) for r in rows]
    for nc in num_cols[:5]:
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


def _render_png_bar(plt, rows, cols, title, out_path):
    _, num_cols, cat_cols = _classify_cols(cols, rows)
    cat_col = cat_cols[0] if cat_cols else cols[0]
    num_col = num_cols[0] if num_cols else next((c for c in cols if c != cat_col), cols[-1])
    sorted_rows = sorted(rows, key=lambda r: _to_float(r.get(num_col)), reverse=True)[:20]
    fig, ax = plt.subplots(figsize=(11, max(4, len(sorted_rows) * 0.35)), dpi=100)
    labels = [_to_str(r.get(cat_col)) for r in sorted_rows]
    values = [_to_float(r.get(num_col)) for r in sorted_rows]
    y_pos = range(len(labels))
    colors = ["#2ca02c" if v > 0 else "#d62728" for v in values]
    ax.barh(y_pos, values, color=colors, alpha=0.8)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel(num_col)
    if title:
        ax.set_title(title, fontsize=13)
    ax.grid(True, axis="x", alpha=0.3)
    for i, v in enumerate(values):
        ax.text(v, i, f" {v:,.0f}", va="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", dpi=100)
    plt.close(fig)
    return out_path


def _render_png_empty(plt, title, output_name):
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
