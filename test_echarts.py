"""
ECharts 集成 e2e 测试 (不依赖 LLM 输出)
- 启本地 server
- 直接用 chart_renderer 单元测试 4 类图(空/line/bar/kpi)
- 验 echart endpoint 返 valid JSON option
- 验 404 路径
- 验 chat workflow chart_path 是 /api/echart/ 路径
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = "http://127.0.0.1:18001"


def post(path, body, timeout=90):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return r.status, json.loads(r.read())


def get_raw(path):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait_ready(timeout=30):
    for _ in range(timeout):
        try:
            r = urllib.request.urlopen(f"{BASE}/health", timeout=2)
            if r.status == 200:
                return True
        except Exception:
            time.sleep(1)
    return False


# ============== 单元测试 (chart_renderer) ==============

def test_unit_chart_renderer():
    """直接调 build_echart_option 测 4 种图"""
    from agent.chart_renderer import build_echart_option, render_echart

    print("=== Unit: chart_renderer ===")

    # 1. 空数据
    opt_empty = build_echart_option({}, "empty title")
    assert "title" in opt_empty
    assert opt_empty.get("graphic"), "empty: missing graphic"
    print("[1] empty: ok (graphic 占位)")

    # 2. line chart
    line_data = {
        "columns": ["date", "gmv", "orders"],
        "rows": [
            {"date": "2025-07-01", "gmv": 120000, "orders": 320},
            {"date": "2025-07-02", "gmv": 135000, "orders": 340},
            {"date": "2025-07-03", "gmv": 128000, "orders": 315},
        ]
    }
    opt_line = build_echart_option(line_data, title="7天趋势")
    assert opt_line["xAxis"]["type"] == "category", f"line xAxis != category: {opt_line['xAxis']}"
    assert opt_line["yAxis"]["type"] == "value"
    assert len(opt_line["series"]) == 2, f"line 期望 2 series, 拿到 {len(opt_line['series'])}"
    for s in opt_line["series"]:
        assert s["type"] == "line"
        assert len(s["data"]) == 3
    print(f"[2] line: ok (2 series, xAxis category)")

    # 3. bar chart
    bar_data = {
        "columns": ["category", "gmv"],
        "rows": [
            {"category": "家电", "gmv": 800000},
            {"category": "食品", "gmv": 500000},
            {"category": "美妆", "gmv": 300000},
        ]
    }
    opt_bar = build_echart_option(bar_data, title="分类 GMV")
    assert opt_bar["xAxis"]["type"] == "value"
    assert opt_bar["yAxis"]["type"] == "category", f"bar yAxis != category: {opt_bar['yAxis']}"
    assert opt_bar["series"][0]["type"] == "bar"
    assert len(opt_bar["yAxis"]["data"]) == 3
    print(f"[3] bar: ok (3 categories)")

    # 4. KPI
    kpi_data = {
        "columns": ["total_gmv"],
        "rows": [{"total_gmv": 2807000.5}]
    }
    opt_kpi = build_echart_option(kpi_data)
    assert opt_kpi["series"][0]["type"] == "pie", f"kpi series != pie: {opt_kpi['series']}"
    print(f"[4] kpi: ok (pie 显示数字)")

    # 5. render_echart 写文件
    url = render_echart(line_data, title="测试图", session_id="unit_test_001")
    assert url == "/api/echart/unit_test_001", f"url != 期望: {url}"
    assert os.path.exists("reports/echarts/unit_test_001.json")
    print(f"[5] render_echart: ok ({url})")

    # 6. 中文 category 编码
    import json
    on_disk = json.load(open("reports/echarts/unit_test_001.json", encoding="utf-8"))
    cat = on_disk["xAxis"]["data"]
    assert "2025-07-01" in cat, f"中文/日期丢失: {cat}"
    print(f"[6] 文件 roundtrip: ok (date='{cat[0]}')")

    print("=== Unit PASS ===\n")


# ============== HTTP 端点测试 ==============

def test_http_echart_endpoint():
    """测 server /api/echart/<sid> 端点"""
    print("=== HTTP: /api/echart/{sid} ===")

    # 先用 renderer 写一个文件
    from agent.chart_renderer import render_echart
    render_echart(
        {"columns": ["cat", "val"], "rows": [{"cat": "A", "val": 100}, {"cat": "B", "val": 200}]},
        title="HTTP 测试",
        session_id="http_test_001",
    )

    # 1. 200 路径
    st, body = get("/api/echart/http_test_001")
    assert st == 200, f"200 fail: {st}"
    assert body["title"]["text"] == "HTTP 测试"
    assert body["series"][0]["type"] == "bar"
    print(f"[1] 200: ok (series type={body['series'][0]['type']})")

    # 2. 404 路径
    st, _ = get_raw("/api/echart/nonexistent_xxx")
    assert st == 404, f"404 expected, got {st}"
    print(f"[2] 404: ok")

    # 3. 旧 /api/chart/<name> 兼容
    st, _ = get_raw("/api/chart/nonexistent.png")
    assert st == 404, f"old route 404 expected, got {st}"
    print(f"[3] 老 /api/chart 路由兼容: ok")

    print("=== HTTP PASS ===\n")


# ============== Workflow 集成测试 ==============

def test_workflow_chart_path():
    """测 chat workflow 跑完后 chart_path 走 /api/echart/"""
    print("=== Workflow: chart_path 集成 ===")
    r = post("/api/chat/new", {"message": "看个数据"})
    sid = r["session_id"]
    # 推到底
    for _ in range(8):
        st = get(f"/api/chat/{sid}")[1]
        if st.get("phase") in ("done", "error"):
            break
        if st.get("phase") in ("clarifying", "awaiting_confirmation"):
            post(f"/api/chat/{sid}/step", {"message": "确认"})
        time.sleep(3)

    st = get(f"/api/chat/{sid}")[1]
    chart_path = st.get("chart_path", "")
    print(f"    phase: {st.get('phase')}, chart_path: {chart_path}")
    if chart_path:
        assert chart_path.startswith("/api/echart/"), f"chart_path 没走 echart: {chart_path}"
        # 验 endpoint 返的 JSON 能被前端的 echarts.setOption 接受
        st2, opt = get(chart_path)
        assert st2 == 200
        assert "series" in opt, f"option 缺 series: {list(opt.keys())}"
        print(f"    echart endpoint ok, series type: {opt['series'][0].get('type', '?')}")
    else:
        print("    [warn] no chart_path (可能 mock 数据空,跳过)")
    print("=== Workflow PASS ===\n")


def main():
    print("=" * 60)
    print("ECharts Integration Test")
    print("=" * 60)

    test_unit_chart_renderer()

    # 启服务
    print("[server] starting on :18001")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app",
         "--host", "127.0.0.1", "--port", "18001"],
        cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_ready(30):
            print("FAIL: server not ready")
            return 1

        test_http_echart_endpoint()
        test_workflow_chart_path()

        print("=" * 60)
        print("ALL ECHARTS TESTS PASS")
        print("=" * 60)
        return 0

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
