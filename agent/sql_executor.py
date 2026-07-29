"""
SQL 执行器 — 安全执行 + 结果格式化
"""
import duckdb
import os
from typing import List, Dict, Any, Tuple

WAREHOUSE_PATH = os.path.join(os.path.dirname(__file__), "..", "warehouse", "ecommerce.duckdb")


class SQLExecutor:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or WAREHOUSE_PATH
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"库不存在: {self.db_path},先跑 warehouse/seed_data.py")
        self.con = duckdb.connect(self.db_path, read_only=True)

    def execute(self, sql: str, max_rows: int = 1000) -> Dict[str, Any]:
        """
        执行 SQL,返回结构化结果
        """
        try:
            # 安全检查:跳过注释,只允许 SELECT/CTE,禁止写操作
            stripped = sql.strip()
            # 去掉开头的 -- 注释行
            while stripped.startswith("--"):
                nl = stripped.find("\n")
                if nl < 0:
                    stripped = ""
                    break
                stripped = stripped[nl+1:].lstrip()
            sql_check = stripped.upper()
            if not (sql_check.startswith("SELECT") or sql_check.startswith("WITH")):
                return {
                    "ok": False,
                    "error": "只允许 SELECT/WITH 查询,拒绝其他语句",
                    "rows": [],
                    "columns": [],
                }

            result = self.con.execute(sql)
            cols = [d[0] for d in result.description] if result.description else []
            rows_raw = result.fetchmany(max_rows)
            rows = [dict(zip(cols, [self._json_safe(v) for v in r])) for r in rows_raw]

            return {
                "ok": True,
                "columns": cols,
                "rows": rows,
                "row_count": len(rows),
                "truncated": len(rows_raw) >= max_rows,
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"SQL 执行失败: {str(e)}",
                "rows": [],
                "columns": [],
            }

    def _json_safe(self, v):
        """处理 Decimal / datetime / 不可序列化对象"""
        if v is None:
            return None
        if isinstance(v, (int, float, str, bool)):
            return v
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return str(v)

    def list_tables(self) -> List[str]:
        rows = self.con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name='main' ORDER BY table_name"
        ).fetchall()
        return [r[0] for r in rows]

    def close(self):
        self.con.close()


if __name__ == "__main__":
    # 简单自检
    exe = SQLExecutor()
    print("可用表:", exe.list_tables())
    r = exe.execute("SELECT COUNT(*) AS n FROM dwd_trade_order")
    print("dwd_trade_order 行数:", r)
    r = exe.execute("SELECT * FROM ads_gmv_daily LIMIT 3")
    print("ads_gmv_daily 样例:", r)
    exe.close()
