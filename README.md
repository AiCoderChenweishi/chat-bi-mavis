# 数据分析 Agent — Agentic Workflow 版

> 一个多轮对话的数据分析 agent:用户说一句需求,自动反问澄清 → 理解数仓 → 生成 SQL → 执行查询 → 出图 → 写结论。

## 📊 真实状态 (Honest Status)

| 维度 | 状态 | 数字/位置 |
|------|------|----------|
| 📐 **designed** | 完整 | 4 个 agent prompt (prompts/01-04) + workflow 状态机 (agent/workflow.py) + 17 张表数仓 schema |
| 💻 **implemented** | 完整 | 11 个 Python 文件,~1,800 行;5 个业务场景 e2e 测试 5/5 通过 |
| 🚀 **deployed** | 本地服务 | FastAPI server,localhost:8000,可访问 UI |
| ✅ **validated** | 端到端 | 5 个场景:GMV/复购/漏斗/券 ROI/留存,SQL 跑出真实数据,图表渲染 OK |

**没做的事**:
- ❌ 没部署到公网 server(只本地运行)
- ❌ 没接真实数仓(MySQL/Hive/ClickHouse),只跑 DuckDB mock
- ❌ mock LLM fallback 简单,真实场景需要 minimax/deepseek 才好用

## 🏗️ 架构

```
用户: "最近 30 天 GMV 怎么样"
     ↓
[Agent 1: 需求澄清] → 多轮反问(最多 3 轮,3 槽位)
     ↓
[Agent 2: 数仓理解] → 看 DWD/DWS/ADS 元数据,选表+定字段
     ↓
[Agent 3: SQL 生成] → DuckDB SQL,5 项自检
     ↓
[SQL 执行] → DuckDB in-process,带 is_mock 过滤
     ↓
[图表生成] → matplotlib 自动选型(线/柱/KPI)
     ↓
[Agent 4: 结论撰写] → 摘要+发现+建议+局限
     ↓
最终回复: markdown 报告 + 图表
```

## 📁 项目结构

```
data-analyst-agent/
├── prompts/                          # 4 个 agent 的 system prompt
│   ├── 01_requirement_clarifier.md   # 需求澄清(多轮反问)
│   ├── 02_warehouse_understander.md  # 数仓理解(选表+口径)
│   ├── 03_sql_generator.md           # SQL 生成(DuckDB)
│   └── 04_conclusion_writer.md       # 结论撰写(业务建议)
├── warehouse/                        # 数仓 mock
│   ├── schema_ddl.sql                # 17 张表 DDL(DWD 6 + DWS 5 + ADS 5)
│   ├── seed_data.py                  # mock 数据生成(明标 is_mock=TRUE)
│   ├── ecommerce.duckdb              # 生成的库(~12 MB,33 万行)
│   └── metadata.json                 # 抽给 LLM 看的元数据
├── agent/                            # 工作流核心
│   ├── llm_client.py                 # LLM 客户端(minimax/deepseek/mock fallback)
│   ├── agents.py                     # 4 个 agent 节点
│   ├── workflow.py                   # 状态机编排
│   ├── sql_executor.py               # SQL 执行器(安全检查)
│   └── chart_renderer.py             # matplotlib 图表(自动选型)
├── static/
│   └── index.html                    # Chat UI(深色主题)
├── tests/
│   └── e2e_test.py                   # 5 个场景端到端测试
├── server.py                         # FastAPI server
├── reports/                          # 生成的图表 PNG
└── README.md
```

## 🚀 快速开始

### 1. 装依赖
```bash
pip install --break-system-packages duckdb openai fastapi uvicorn jinja2 pydantic
```

### 2. 建数仓
```bash
cd warehouse
python3 seed_data.py     # 3 秒建好,33 万行
python3 metadata_extractor.py  # 抽元数据
```

### 3. 启动 server
```bash
# 无 API key:走 mock LLM
python3 server.py

# 有 API key:走真实 LLM(minimax 优先,deepseek 兜底)
export MINIMAX_API_KEY=xxx
export DEEPSEEK_API_KEY=yyy  # 可选
python3 server.py
```

打开 http://localhost:8000

### 4. 跑测试
```bash
python3 tests/e2e_test.py --mock  # 强制 mock,5/5 通过
python3 tests/e2e_test.py          # 真实 LLM
```

## 💡 业务场景示例

| 用户输入 | 走的表 | 出的数据 |
|---------|-------|---------|
| "最近 30 天 GMV" | ads_gmv_daily | 5 个品类 GMV 排名 + 同比 |
| "618 复购率掉了" | dwd_trade_order | 复购率单值 + 总用户/复购用户 |
| "新客转化漏斗" | ads_conversion_funnel | 4 个渠道的 4 步转化率 |
| "券 ROI" | ads_coupon_roi | 3 种券的 ROI + 带动 GMV |
| "用户留存" | ads_user_retention | 1/7/30 日留存率 |
| "A 类目哪个 SKU 卖得最好" | dws_product_sale_day | SKU 销量 TopN |

## 🧠 4 个 Agent 角色职责

### Agent 1: 需求澄清(Requirement Clarifier)
- **输入**: 用户的一句话需求
- **职责**: 反问 1-3 轮,把模糊需求拆成 4 槽位:time_range / dimensions / metrics / comparison
- **输出**: 严格 JSON spec
- **反问规则**: 每轮 ≤2 问,优先问最有歧义的,3 轮上限

### Agent 2: 数仓理解(Warehouse Understander)
- **输入**: JSON spec + 元数据(表/字段/注释)
- **职责**: 选表优先级 ADS > DWS > DWD,定口径,标风险
- **输出**: selected_tables + join + fields + filters + risks + confidence

### Agent 3: SQL 生成(SQL Generator)
- **输入**: spec + 数仓理解结果
- **职责**: 写 DuckDB SQL,5 项自检(语法/字段/join/时间/mock 过滤)
- **输出**: SQL + 自检报告 + 失败 fallback SQL

### Agent 4: 结论撰写(Conclusion Writer)
- **输入**: SQL 结果 + spec + 图表路径
- **职责**: 写业务可读的 markdown 报告
- **输出**: 摘要+发现+建议+局限+追问方向

## 🔧 Mock 数据规模

| 表 | 行数 | 说明 |
|---|------|------|
| dwd_user_register | 5,000 | 用户注册 |
| dwd_product_sku | 6,000 | 商品 SKU(5 大类 × 20 中类 × 3 小类 × 20) |
| dwd_trade_order | 80,000 | 订单明细(60% 已支付) |
| dwd_coupon_use | ~24,000 | 券使用 |
| dwd_traffic_visit | 200,000 | 流量访问 |
| dwd_user_login | 30,000 | 登录行为 |
| dws_trade_user_day | ~12,500 | 交易×用户×日 |
| ads_gmv_daily | ~11,500 | 日 GMV(含同比) |
| ads_category_rank | ~33,000 | 类目 GMV 排名 |
| ads_coupon_roi | ~1,700 | 券 ROI |
| ads_conversion_funnel | ~2,300 | 转化漏斗 |
| ads_user_retention | ~1,500 | 用户留存 |

**总计 ~ 41 万行,时间范围 2025-01-01 ~ 2026-07-31(1.5 年,含去年 30 天可做同比)**

## ⚠️ Mock 标注

所有数据生成时硬编码 `is_mock = TRUE`,SQL 强制带 `is_mock = TRUE` 过滤。结论中强制写"数据为 mock 合成"。**严禁当真实业务数据用**。

## 🐛 已知坑(踩过的)

- DuckDB `ELT()` 不存在 → 用数组字面量 `['a','b'][n]`
- DuckDB `CHAR(N)` 不接受算术 → 改用 `CHR(CAST(N AS INTEGER))`
- DuckDB `UNNEST(arr1, arr2)` 多 array 不支持 → 拆 3 个 subquery 再 cross join
- DuckDB `BETWEEN` 对 DECIMAL 列默认值要 DECIMAL(10,2)+,不然 yoy_pct 33992 装不进
- LLM 返回 None → 加多层 try/except 兜底到 mock
- SQL 注释 `--` 开头要 strip 掉再判 SELECT/WITH
- mock LLM 用 `_call_mock` 接 user 字符串时,要从 prompt 抽"原始问题"段,不能拿最后一轮 user 答

## 📈 后续可做(未做)

- [ ] 接入真实 MySQL/Hive 数仓(只需替换 sql_executor)
- [ ] 多轮追问支持("想看 A 类目下钻")
- [ ] SQL 执行慢的 query 加 timeout + 提示
- [ ] 图表支持更多类型(漏斗/热力/桑基)
- [ ] 结论支持导出 PDF/PPT
- [ ] 用户管理(多租户、session 持久化)
