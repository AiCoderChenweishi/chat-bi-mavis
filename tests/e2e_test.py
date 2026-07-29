"""
E2E 测试 — 验证 5 个业务场景跑通
- 不开 server,直接调 workflow
- 每个场景: 输入 → 跑完 → 检 final state 字段
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.workflow import DataAnalystWorkflow


SCENARIOS = [
    {
        "name": "GMV 总体分析",
        "input": "最近 30 天 GMV 怎么样",
        "expect_in_spec": "GMV",
        "expect_table": "ads_gmv_daily",
    },
    {
        "name": "复购率分析",
        "input": "618 复购率掉了帮我看下",
        "expect_in_spec": "复购率",
        "expect_table": "dwd_trade_order",  # 复购无 ADS 直接覆盖,需走 DWD
    },
    {
        "name": "转化漏斗",
        "input": "新客转化漏斗做一下",
        "expect_in_spec": "转化",
        "expect_table": "ads_conversion_funnel",
    },
    {
        "name": "券 ROI",
        "input": "券 ROI 怎么样",
        "expect_in_spec": "ROI",
        "expect_table": "ads_coupon_roi",
    },
    {
        "name": "用户留存",
        "input": "用户留存情况",
        "expect_in_spec": "留存",
        "expect_table": "ads_user_retention",
    },
]


def run_scenario(workflow, scenario):
    print(f"\n{'='*60}")
    print(f"📋 场景: {scenario['name']}")
    print(f"   输入: {scenario['input']}")
    print(f"{'='*60}")

    st = workflow.new_session(scenario["input"])
    # 1 轮:触发澄清
    workflow.step(st.session_id, scenario["input"])
    print(f"   [澄清后] phase={st.phase} round={st.round}")
    print(f"   [反问消息] {st.assistant_message[:200]}")

    # 如果还在 clarify,模拟用户回答 "默认就行"
    if st.phase == "clarifying":
        workflow.step(st.session_id, "默认就行,30 天")
        print(f"   [回答后] phase={st.phase}")
        if st.phase == "clarifying":
            workflow.step(st.session_id, "你看着办,出数据")
            print(f"   [回答后] phase={st.phase}")

    # 跑到底
    if st.phase not in ("done", "error"):
        st = workflow.run_through(st.session_id)

    # 检
    print(f"   [终态] phase={st.phase} llm_calls={st.llm_calls}")
    if st.phase == "error":
        print(f"   ❌ 错误: {st.error}")
        return False

    # 检 spec 含预期指标
    spec_str = str(st.spec or {})
    if scenario["expect_in_spec"] not in spec_str:
        print(f"   ⚠️  spec 缺 {scenario['expect_in_spec']!r}")
        print(f"   spec={st.spec}")
    else:
        print(f"   ✓ spec 含 {scenario['expect_in_spec']!r}")

    # 检选表
    plan = st.warehouse_plan or {}
    tables = [t["name"] for t in plan.get("selected_tables", [])]
    print(f"   ✓ 选表: {tables} (confidence: {plan.get('confidence')})")
    if scenario["expect_table"] not in str(tables):
        print(f"   ⚠️  预期表 {scenario['expect_table']!r} 不在选表列表中(LLM 可能选了其他表)")

    # 检 SQL
    print(f"   ✓ SQL 长度: {len(st.sql or '')} 字符")
    print(f"   ✓ SQL 预览:\n     {st.sql.split(chr(10))[0] if st.sql else 'NONE'}")

    # 检执行结果
    res = st.sql_result or {}
    print(f"   ✓ SQL 执行: ok={res.get('ok')} 行数={res.get('row_count')} 列数={len(res.get('columns', []))}")
    if not res.get("ok"):
        print(f"   ❌ SQL 错误: {res.get('error')}")
        return False
    if not res.get("rows"):
        print(f"   ❌ SQL 无数据")
        return False

    # 检图表
    if st.chart_path:
        print(f"   ✓ 图表: {st.chart_path}")
    else:
        print(f"   ⚠️  无图表")

    # 检结论
    c = st.conclusion or {}
    print(f"   ✓ 结论摘要: {c.get('summary', '')[:80]}...")
    print(f"   ✓ 关键发现: {len(c.get('key_findings', []))} 条")
    print(f"   ✓ 业务建议: {len(c.get('business_recommendations', []))} 条")
    print(f"   ✓ 数据局限: {len(c.get('data_limitations', []))} 条")

    print(f"\n   {'✅ PASS' if (st.phase == 'done' and res.get('ok') and res.get('rows')) else '❌ FAIL'}")
    return st.phase == "done" and res.get("ok") and res.get("rows")


def main():
    force_mock = "--mock" in sys.argv
    if force_mock:
        # 强制用 mock LLM
        os.environ.pop("MINIMAX_API_KEY", None)
        os.environ.pop("DEEPSEEK_API_KEY", None)
    print("🚀 启动 E2E 测试")
    print(f"   LLM: {'mock' if force_mock or (not os.environ.get('MINIMAX_API_KEY') and not os.environ.get('DEEPSEEK_API_KEY')) else 'live'}")
    workflow = DataAnalystWorkflow()
    print(f"   provider: {workflow.llm.provider}")

    results = []
    for sc in SCENARIOS:
        try:
            ok = run_scenario(workflow, sc)
            results.append((sc["name"], ok))
        except Exception as e:
            print(f"   ❌ 异常: {e}")
            import traceback; traceback.print_exc()
            results.append((sc["name"], False))

    print(f"\n\n{'='*60}")
    print("📊 测试结果汇总")
    print(f"{'='*60}")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'}  {name}")
    print(f"\n  通过: {passed}/{len(results)}")

    return passed == len(results)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
