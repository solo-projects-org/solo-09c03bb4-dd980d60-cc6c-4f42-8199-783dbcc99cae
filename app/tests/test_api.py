"""Flask API 层测试：路由、错误码与样例端到端。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


client = app.test_client()
SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "sample_clearing.json")
with open(SAMPLE, encoding="utf-8") as fh:
    sample = json.load(fh)

# 首页
r = client.get("/")
check("首页 200", r.status_code == 200 and "换乘标志".encode() in r.data)

# 样例接口
r = client.get("/api/sample")
check("样例接口", r.status_code == 200 and r.get_json()["models"])

# 校验通过
r = client.post("/api/validate", json=sample)
body = r.get_json()
check("validate 通过", r.status_code == 200 and body["ok"] is True)
check("validate 返回源表", len(body["source_tables"]) == 3)
check("validate 返回模型依赖",
      {m["name"]: m["upstream"] for m in body["models"]}["ads_transfer_settlement"]
      == ["dwd_card_transaction_detail", "dws_line_transfer_summary"])

# 分析
r = client.post("/api/analyze?table=ods_card_transaction&column=transfer_flag", json=sample)
body = r.get_json()
check("analyze 通过", r.status_code == 200 and body["ok"] is True)
check("analyze 命中 3 模型", body["result"]["model_count"] == 3)
check("analyze 命中 19 列", body["result"]["column_count"] == 19)

# 缺参数
r = client.post("/api/analyze", json=sample)
check("缺参数 400", r.status_code == 400 and r.get_json()["ok"] is False)

# 坏 JSON
r = client.post("/api/validate", data="{oops", content_type="application/json")
check("坏 JSON 400", r.status_code == 400 and "JSON" in r.get_json()["error"])

# 空 body
r = client.post("/api/validate", data="", content_type="application/json")
check("空 body 400", r.status_code == 400 and "空" in r.get_json()["error"])

# 无法解析的引用 -> 指出模型并停止
bad = {
    "source_tables": [{"table": "t", "columns": ["a"]}],
    "models": [{"name": "boom", "sql": "SELECT z.a AS a FROM t x"}],
}
r = client.post("/api/validate", json=bad)
check("未知别名 400 带模型名",
      r.status_code == 400 and "boom" in r.get_json()["error"] and "z" in r.get_json()["error"])

# 非字典顶层
r = client.post("/api/validate", json=[1, 2, 3])
check("顶层非对象 400", r.status_code == 400)

# 目标字段不存在
r = client.post("/api/analyze?table=ods_card_transaction&column=ghost", json=sample)
check("目标列不存在 400", r.status_code == 400 and "ghost" in r.get_json()["error"])

# 未被任何模型读取的字段 -> 零影响
r = client.post("/api/analyze?table=ods_card_info&column=issue_city", json=sample)
body = r.get_json()
check("未读取字段零影响",
      r.status_code == 200 and body["result"]["model_count"] == 0)

# ---------------------------------------------------------------------------
# 复算裁剪
# ---------------------------------------------------------------------------
PRUNE_SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "sample_prune.json")
with open(PRUNE_SAMPLE, encoding="utf-8") as fh:
    prune_sample = json.load(fh)

# 裁剪样例接口
r = client.get("/api/sample_prune")
body = r.get_json()
check("裁剪样例接口", r.status_code == 200
      and "ads_transfer_settlement" in [m["name"] for m in body["models"]]
      and "dws_line_amount_daily" in [m["name"] for m in body["models"]]
      and "dws_card_transfer_daily" in [m["name"] for m in body["models"]])

# validate 返回模型输出列（供裁剪勾选）
r = client.post("/api/validate", json=prune_sample)
body = r.get_json()
check("validate 返回输出列",
      {m["name"]: m["outputs"] for m in body["models"]}["ads_transfer_settlement"]
      == ["line_id", "line_name", "settle_amount", "card_type", "transfer_flag", "settle_date"])

# 全列裁剪：两分支并集
all_cols = "line_id,line_name,settle_amount,card_type,transfer_flag,settle_date"
r = client.post(f"/api/prune?model=ads_transfer_settlement&columns={all_cols}",
                json=prune_sample)
body = r.get_json()
check("prune 通过", r.status_code == 200 and body["ok"] is True)
pkg = body["result"]["package"]
summ = body["result"]["summary"]
check("prune 丢弃无关模型",
      [m["name"] for m in pkg["models"]]
      == ["dwd_card_transaction_detail", "dws_line_amount_daily",
          "dws_card_transfer_daily", "ads_transfer_settlement"]
      and summ["dropped_models"] == ["ads_line_passenger_flow"])
dwd_sql = next(m["sql"] for m in pkg["models"] if m["name"] == "dwd_card_transaction_detail")
check("prune 上游裁掉未用列", "transaction_id" not in dwd_sql and "issue_city" not in dwd_sql)
check("prune 连接筛选保留",
      "INNER JOIN ods_card_info" in dwd_sql and "WHERE t.city_code = '3301'" in dwd_sql)
txn_cols = next(t["columns"] for t in pkg["source_tables"] if t["table"] == "ods_card_transaction")
check("prune 源表字段收窄",
      txn_cols == ["card_id", "line_id", "amount", "transfer_flag", "city_code", "settle_date"],
      str(txn_cols))

# 子集裁剪：列序保持原相对顺序
r = client.post("/api/prune?model=ads_transfer_settlement&columns=settle_date,settle_amount",
                json=prune_sample)
body = r.get_json()
ads_sql = next(m["sql"] for m in body["result"]["package"]["models"]
               if m["name"] == "ads_transfer_settlement")
check("prune 列序不变",
      ads_sql.index("settle_amount") < ads_sql.index("settle_date"), ads_sql)
card_sql = next(m["sql"] for m in body["result"]["package"]["models"]
                if m["name"] == "dws_card_transfer_daily")
check("prune 分支未用列被裁", "card_type" not in card_sql, card_sql)

# 精简包可回灌 validate（现有规则校验通过的旁证）
r2 = client.post("/api/validate", json=pkg)
check("精简包可回灌校验", r2.status_code == 200 and r2.get_json()["ok"] is True)

# CTE 样例用于裁剪 -> 400 说明原因
r = client.post("/api/prune?model=ads_transfer_settlement&columns=line_id", json=sample)
body = r.get_json()
check("prune 拒绝 CTE", r.status_code == 400 and "CTE" in body["error"], body.get("error"))

# 缺参数
r = client.post("/api/prune?columns=line_id", json=prune_sample)
check("prune 缺模型 400", r.status_code == 400 and "结算模型" in r.get_json()["error"])
r = client.post("/api/prune?model=ads_transfer_settlement", json=prune_sample)
check("prune 缺列 400", r.status_code == 400 and "输出列" in r.get_json()["error"])

# 选择不存在的输出列
r = client.post("/api/prune?model=ads_transfer_settlement&columns=ghost", json=prune_sample)
check("prune 幽灵列 400", r.status_code == 400 and "ghost" in r.get_json()["error"])

# 坏 JSON
r = client.post("/api/prune?model=x&columns=y", data="{oops", content_type="application/json")
check("prune 坏 JSON 400", r.status_code == 400 and "JSON" in r.get_json()["error"])

print()
if FAILURES:
    print(f"{len(FAILURES)} 项失败：{FAILURES}")
    sys.exit(1)
print("API 测试全部通过。")
