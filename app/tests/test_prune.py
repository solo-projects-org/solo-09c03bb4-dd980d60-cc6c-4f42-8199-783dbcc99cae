"""prune.py 测试：复算裁剪规则、值/列序/重复行保持、只读行处理与错误处理。"""
import json
import os
import sys

import sqlglot

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lineage import AnalysisError, analyze  # noqa: E402
from prune import ROW_ONLY_COLUMN, prune_package  # noqa: E402

FAILURES = []
DIALECT = "duckdb"


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def outputs_of(sql):
    tree = sqlglot.parse_one(sql, read=DIALECT)
    return [p.alias_or_name for p in tree.expressions]


def clauses_of(sql):
    """返回（FROM, [JOIN...], WHERE）的规范化 SQL，用于比较连接与筛选是否原样保留。"""
    tree = sqlglot.parse_one(sql, read=DIALECT)
    where = tree.args.get("where")
    return (
        tree.args["from_"].sql(DIALECT),
        [j.sql(DIALECT) for j in tree.args.get("joins", [])],
        where.sql(DIALECT) if where is not None else None,
    )


def projections_of(sql):
    tree = sqlglot.parse_one(sql, read=DIALECT)
    return {p.alias_or_name: p.sql(DIALECT) for p in tree.expressions}


def expect_error(name, fn, keyword):
    try:
        fn()
        check(name, False, "未抛出 AnalysisError")
    except AnalysisError as e:
        check(name, keyword in str(e), str(e))


SAMPLE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "sample_prune.json")
with open(SAMPLE_PATH, encoding="utf-8") as fh:
    SAMPLE = json.load(fh)

ALL_ADS_COLS = ["line_id", "line_name", "settle_amount", "card_type", "transfer_flag", "settle_date"]

# 1. 全列复算：只保留复算链路上的模型与源表 ------------------------------------------------
r = prune_package(SAMPLE, "ads_transfer_settlement", ALL_ADS_COLS)
pkg, s = r["package"], r["summary"]
kept_model_names = [m["name"] for m in pkg["models"]]
check("链路-无关模型被丢弃",
      kept_model_names == ["dwd_card_transaction_detail", "dws_line_amount_daily",
                           "dws_card_transfer_daily", "ads_transfer_settlement"]
      and s["dropped_models"] == ["ads_line_passenger_flow"], str(kept_model_names))
check("链路-源表全部保留", [t["table"] for t in pkg["source_tables"]]
      == ["ods_card_transaction", "ods_card_info", "dim_bus_line"])
check("链路-顶层其它字段保留", pkg.get("scenario") == SAMPLE["scenario"])

# 2. 两分支并集：同一上游被两个分支读取，留齐各分支用到的列 ----------------------------------
# 注：dwd.card_id 的唯一读取者是 dws_card 的投影 d.card_id，该投影本身已被裁掉
# （ads 不读 b.card_id），因此 dwd.card_id 也被传递裁掉；t.card_id 仍作为
# 连接引用列保留在源表字段清单中。
dwd = next(m for m in pkg["models"] if m["name"] == "dwd_card_transaction_detail")
check("并集-上游留齐两分支列",
      outputs_of(dwd["sql"]) == ["line_id", "amount", "transfer_flag",
                                 "settle_date", "card_type", "line_name"],
      str(outputs_of(dwd["sql"])))
tbl = {t["table"]: t["columns"] for t in pkg["source_tables"]}
check("并集-源表列按原顺序裁剪",
      tbl["ods_card_transaction"] == ["card_id", "line_id", "amount", "transfer_flag",
                                      "city_code", "settle_date"]
      and tbl["ods_card_info"] == ["card_id", "card_type", "status"]
      and tbl["dim_bus_line"] == ["line_id", "line_name"], str(tbl))

# 3. 子集复算：所选列按原列序保留，未用列继续向下裁剪 ----------------------------------------
r2 = prune_package(SAMPLE, "ads_transfer_settlement", ["settle_date", "settle_amount"])
pkg2, s2 = r2["package"], r2["summary"]
out2 = {m["name"]: outputs_of(m["sql"]) for m in pkg2["models"]}
check("子集-结算模型列序不变", out2["ads_transfer_settlement"] == ["settle_amount", "settle_date"],
      str(out2["ads_transfer_settlement"]))
check("子集-连接列自动保留",
      out2["dws_line_amount_daily"] == ["line_id", "amount", "settle_date"]
      and out2["dws_card_transfer_daily"] == ["transfer_flag", "line_id", "settle_date"],
      str(out2))
check("子集-筛选引用列保留在上游",
      out2["dwd_card_transaction_detail"] == ["line_id", "amount",
                                              "transfer_flag", "settle_date"],
      str(out2["dwd_card_transaction_detail"]))
tbl2 = {t["table"]: t["columns"] for t in pkg2["source_tables"]}
check("子集-源表列进一步收窄",
      tbl2["ods_card_info"] == ["card_id", "status"] and tbl2["dim_bus_line"] == ["line_id"],
      str(tbl2))

# 4. 连接与筛选原样保留（重复行不变）；保留列表达式与原文一致（值不变） ------------------------
orig_sql = {m["name"]: m["sql"] for m in SAMPLE["models"]}
for m in pkg["models"]:
    name = m["name"]
    check(f"保持-{name}连接筛选不变", clauses_of(m["sql"]) == clauses_of(orig_sql[name]),
          f"{clauses_of(m['sql'])} != {clauses_of(orig_sql[name])}")
    orig_proj = projections_of(orig_sql[name])
    slim_proj = projections_of(m["sql"])
    check(f"保持-{name}保留列表达式一致",
          all(slim_proj[c] == orig_proj[c] for c in slim_proj))
    check(f"保持-{name}无去重", "DISTINCT" not in m["sql"].upper())

# 5. 精简包可继续跑现有血缘分析（现有规则校验通过的旁证） --------------------------------------
ra = analyze(pkg, "ods_card_transaction", "amount")
check("精简包-可再做血缘分析", ra["model_count"] >= 1)

# 6. 只读行不取列：上游模型保留一个常量列 -----------------------------------------------------
p_rows = {
    "source_tables": [
        {"table": "t1", "columns": ["id", "k", "v"]},
        {"table": "t2", "columns": ["id", "w"]},
    ],
    "models": [
        {"name": "m1", "sql": "SELECT a.id AS id, a.k AS k, a.v AS v FROM t1 a"},
        {"name": "m_rows", "sql": "SELECT b.id AS id, b.w AS w FROM t2 b WHERE b.w > 0"},
        {"name": "m2", "sql":
         "SELECT x.id AS id, x.v AS v FROM m1 x INNER JOIN m_rows y ON x.k = 1"},
    ],
}
r3 = prune_package(p_rows, "m2", ["id", "v"])
pkg3, s3 = r3["package"], r3["summary"]
m_rows_sql = next(m["sql"] for m in pkg3["models"] if m["name"] == "m_rows")
check("只读行-上游模型常量列", outputs_of(m_rows_sql) == [ROW_ONLY_COLUMN], m_rows_sql)
check("只读行-常量列模型筛选保留",
      clauses_of(m_rows_sql) == clauses_of(p_rows["models"][1]["sql"]))
m_rows_sum = next(m for m in s3["models"] if m["name"] == "m_rows")
check("只读行-汇总标记row_only", m_rows_sum["row_only"] is True
      and m_rows_sum["dropped_columns"] == ["id", "w"])
tbl3 = {t["table"]: t["columns"] for t in pkg3["source_tables"]}
check("只读行-其源表仍按引用裁列", tbl3["t2"] == ["w"] and tbl3["t1"] == ["id", "k", "v"], str(tbl3))
m1_sql = next(m["sql"] for m in pkg3["models"] if m["name"] == "m1")
check("只读行-连接引用列进入上游投影", outputs_of(m1_sql) == ["id", "k", "v"])

# 7. 只读行不取列：源表字段清单为空 -----------------------------------------------------------
p_empty = {
    "source_tables": [
        {"table": "t1", "columns": ["id", "k"]},
        {"table": "t2", "columns": ["id", "w"]},
    ],
    "models": [
        {"name": "m1", "sql": "SELECT a.id AS id FROM t1 a INNER JOIN t2 b ON a.k = 1"},
    ],
}
r4 = prune_package(p_empty, "m1", ["id"])
tbl4 = {t["table"]: t["columns"] for t in r4["package"]["source_tables"]}
check("只读行-源表字段清单为空", tbl4["t2"] == [] and tbl4["t1"] == ["id", "k"], str(tbl4))

# 8. 常量投影：可选中、可下传；常量不消耗上游列 ----------------------------------------------
p_const = {
    "source_tables": [{"table": "t1", "columns": ["id", "v"]}],
    "models": [
        {"name": "m1", "sql": "SELECT a.id AS id, 1 AS ver, '3301' AS city FROM t1 a"},
        {"name": "m2", "sql": "SELECT x.id AS id, x.city AS city FROM m1 x"},
    ],
}
r5 = prune_package(p_const, "m2", ["city"])
out5 = {m["name"]: outputs_of(m["sql"]) for m in r5["package"]["models"]}
check("常量-常量列可复算", out5["m1"] == ["city"] and out5["m2"] == ["city"], str(out5))
m1_const_sql = next(m["sql"] for m in r5["package"]["models"] if m["name"] == "m1")
check("常量-常量表达式保留", "'3301' AS city" in m1_const_sql, m1_const_sql)
tbl5 = {t["table"]: t["columns"] for t in r5["package"]["source_tables"]}
check("常量-源表只读行字段为空", tbl5["t1"] == [], str(tbl5))

# 9. 同一查询内自连接同一上游：两个别名的列取并集 ---------------------------------------------
p_self = {
    "source_tables": [{"table": "t1", "columns": ["id", "a", "b", "c"]}],
    "models": [
        {"name": "m1", "sql": "SELECT t.id AS id, t.a AS a, t.b AS b, t.c AS c FROM t1 t"},
        {"name": "m2", "sql":
         "SELECT x.a AS a, y.b AS b FROM m1 x INNER JOIN m1 y ON x.id = y.id"},
    ],
}
r6 = prune_package(p_self, "m2", ["a", "b"])
m1_self_sql = next(m["sql"] for m in r6["package"]["models"] if m["name"] == "m1")
check("自连接-两别名列并集", outputs_of(m1_self_sql) == ["id", "a", "b"], m1_self_sql)

# 10. 不支持的写法：说明原因，不给下载 ---------------------------------------------------------
def bad(models, cols=("x",), target="m1", sources=None):
    payload = {
        "source_tables": sources or [{"table": "t1", "columns": ["id", "x", "y"]}],
        "models": models,
    }
    return lambda: prune_package(payload, target, list(cols))

expect_error("拒绝-CTE", bad([{"name": "m1", "sql":
    "WITH c AS (SELECT a.x AS x FROM t1 a) SELECT c.x AS x FROM c"}]), "CTE")
expect_error("拒绝-FROM子查询", bad([{"name": "m1", "sql":
    "SELECT s.x AS x FROM (SELECT a.x AS x FROM t1 a) s"}]), "子查询")
expect_error("拒绝-IN子查询", bad([{"name": "m1", "sql":
    "SELECT a.x AS x FROM t1 a WHERE a.id IN (SELECT b.id AS id FROM t1 b)"}]), "子查询")
expect_error("拒绝-DISTINCT", bad([{"name": "m1", "sql":
    "SELECT DISTINCT a.x AS x FROM t1 a"}]), "去重")
expect_error("拒绝-GROUP BY", bad([{"name": "m1", "sql":
    "SELECT a.x AS x FROM t1 a GROUP BY a.x"}]), "聚合")
expect_error("拒绝-聚合函数", bad([{"name": "m1", "sql":
    "SELECT SUM(a.x) AS x FROM t1 a"}]), "聚合")
expect_error("拒绝-投影表达式", bad([{"name": "m1", "sql":
    "SELECT a.x + 1 AS x FROM t1 a"}]), "投影只允许")
expect_error("拒绝-CASE投影", bad([{"name": "m1", "sql":
    "SELECT CASE WHEN a.x = 1 THEN 'A' ELSE 'B' END AS x FROM t1 a"}]), "投影只允许")
expect_error("拒绝-WHERE中OR", bad([{"name": "m1", "sql":
    "SELECT a.x AS x FROM t1 a WHERE a.x = 1 OR a.y = 2"}]), "AND")
expect_error("拒绝-条件含函数", bad([{"name": "m1", "sql":
    "SELECT a.x AS x FROM t1 a WHERE LENGTH(a.x) > 1"}]), "列或常量")
expect_error("拒绝-LEFT JOIN", bad([{"name": "m1", "sql":
    "SELECT a.x AS x FROM t1 a LEFT JOIN t1 b ON a.id = b.id"}]), "INNER")
expect_error("拒绝-星号", bad([{"name": "m1", "sql": "SELECT * FROM t1 a"}]), "星号")
expect_error("拒绝-限定星号", bad([{"name": "m1", "sql": "SELECT a.* FROM t1 a"}]), "星号")
expect_error("拒绝-ORDER BY", bad([{"name": "m1", "sql":
    "SELECT a.x AS x FROM t1 a ORDER BY x"}]), "排序")
expect_error("拒绝-LIMIT", bad([{"name": "m1", "sql":
    "SELECT a.x AS x FROM t1 a LIMIT 5"}]), "LIMIT")
expect_error("拒绝-JOIN缺ON", bad([{"name": "m1", "sql":
    "SELECT a.x AS x FROM t1 a INNER JOIN t1 b"}]), "ON")
expect_error("拒绝-UNION", bad([{"name": "m1", "sql":
    "SELECT a.x AS x FROM t1 a UNION SELECT b.x AS x FROM t1 b"}]), "集合运算")
expect_error("拒绝-窗口", bad([{"name": "m1", "sql":
    "SELECT ROW_NUMBER() OVER (ORDER BY a.x) AS x FROM t1 a"}]), "窗口")
expect_error("拒绝-未限定列", bad([{"name": "m1", "sql": "SELECT x AS x FROM t1 a"}]), "未带表名")
expect_error("拒绝-USING", bad([{"name": "m1", "sql":
    "SELECT a.x AS x FROM t1 a INNER JOIN t1 b USING (id)"}]), "USING")

# 11. 选择参数错误 ----------------------------------------------------------------------------
expect_error("拒绝-空选择", bad([{"name": "m1", "sql": "SELECT a.x AS x FROM t1 a"}], cols=[]),
             "至少选择一个")
expect_error("拒绝-选择不存在列",
             bad([{"name": "m1", "sql": "SELECT a.x AS x FROM t1 a"}], cols=["ghost"]),
             "没有输出列")
expect_error("拒绝-模型不存在",
             bad([{"name": "m1", "sql": "SELECT a.x AS x FROM t1 a"}], target="m_ghost"),
             "不在 models")

# 12. 复算链路之外的模型写法不受支持时不影响裁剪（被丢弃） --------------------------------------
p_lazy = {
    "source_tables": [{"table": "t1", "columns": ["id", "x"]}],
    "models": [
        {"name": "m1", "sql": "SELECT a.id AS id, a.x AS x FROM t1 a"},
        {"name": "m2", "sql": "SELECT x.id AS id FROM m1 x"},
        {"name": "m_other", "sql":
         "WITH c AS (SELECT a.id AS id FROM t1 a) SELECT c.id AS id FROM c"},
    ],
}
r7 = prune_package(p_lazy, "m2", ["id"])
check("链路外-不支持写法被丢弃不拦截",
      [m["name"] for m in r7["package"]["models"]] == ["m1", "m2"]
      and r7["summary"]["dropped_models"] == ["m_other"])
# 但链路内的 CTE 必须拦截
expect_error("链路内-CTE仍拦截", bad([
    {"name": "m1", "sql": "SELECT a.id AS id, a.x AS x FROM t1 a"},
    {"name": "m2", "sql": "WITH c AS (SELECT x.id AS id FROM m1 x) SELECT c.id AS id FROM c"},
], cols=("id",), target="m2"), "CTE")

# 13. 样例可完整裁剪（端到端） ------------------------------------------------------------------
check("样例-裁剪结果通过现有规则校验", pkg["models"] and pkg["source_tables"])
r8 = prune_package(SAMPLE, "ads_transfer_settlement", ["settle_amount"])
check("样例-单列复算", outputs_of(next(m for m in r8["package"]["models"]
      if m["name"] == "ads_transfer_settlement")["sql"]) == ["settle_amount"])

print()
if FAILURES:
    print(f"{len(FAILURES)} 项失败：{FAILURES}")
    sys.exit(1)
print("全部测试通过。")
