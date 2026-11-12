# 公交清分数仓 · 血缘分析与复算裁剪

面向公交清分工程师的两个配套工具，共用同一份模型 JSON（源表字段清单 + 各模型 SQL）：

1. **换乘标志口径调整影响分析**：从选中的源表字段出发，沿模型血缘传播，
   给出受影响的**模型**与每个模型的**受影响输出列**，每列附一条可回溯到源表字段的依赖路径，
   逐跳标注是「值传递」还是「条件污染」。
2. **复算裁剪打包**：历史刷卡流水缺档、需要向运营方补取复算字段时，
   选定**结算模型**和**要复算的输出列**，把模型包裁成同格式的**精简包**下载——
   只保留复算用到的模型、源表和列。

本工具用 [SQLGlot](https://github.com/tobymao/sqlglot) 解析 DuckDB SQL，
纯本地运行，**只解析 SQL、不连接数仓也不执行 SQL**；网页可一键载入两个清分样例。

## 复算裁剪口径

### 支持的 SQL 范围（扁平查询）

- 查询只用 `SELECT`、`FROM`、`INNER JOIN ... ON ...`、`WHERE`；
- 投影只用**带限定名的列或常量**，可用 `AS` 改名；
- 连接与筛选只用**列或常量的比较**（`=`、`<>`、`<`、`<=`、`>`、`>=`），可用 `AND` 连接；
- 不支持 CTE、子查询、`DISTINCT` 去重、聚合（`GROUP BY`/聚合函数）、窗口、
  集合运算、`ORDER BY`/`LIMIT` 等，遇到即说明原因并拒绝生成下载。

### 裁剪规则

1. **只保留复算链路**：从选定结算模型沿 `FROM/JOIN` 读取关系向上的全部模型，
   以及这些模型读到的源表；链路外的模型与源表整体丢弃（不参与校验）。
2. **列需求反向传播**：某模型被保留下游读取的输出列 + 其自身连接/筛选引用的列，
   构成对上游的列需求；**同一上游被多个分支读取时，留齐各分支用到的列的并集**。
3. **连接与筛选原样保留**：各模型的 `FROM/JOIN ON/WHERE` 一字不改，只裁剪投影列表，
   且保留列按原投影顺序输出——因此**所选列的值、列序和重复行都不变**。
4. **只读行不取列**：下游只在 `FROM/JOIN` 中挂接、不取任何列时，
   上游模型保留一个常量列（`SELECT 1 AS _keep_row ...`，行数与重复行不变），
   源表的字段清单可为空（`"columns": []`）。
5. **下载前校验**：精简包先用血缘分析的现有规则（引用解析、列存在性、DAG）再校验一遍，
   并对每个模型自检「连接/筛选未变、保留列表达式与列序未变」；
   写法不受支持或校验失败时说明原因，**不提供下载**。

## 影响分析口径

### 支持的 SQL 范围

- 普通 `SELECT`、**非递归** CTE（`WITH`）、`INNER JOIN`、`WHERE`；
- 禁止星号（`SELECT *` / `t.*`），所有列引用必须带表名或别名；
- 支持 `WHERE ... IN (SELECT ...)` / `WHERE EXISTS (SELECT ...)` 形式的非关联谓词子查询、
  FROM 派生表（需别名）；投影中允许任意函数、运算、`CASE`、类型转换等表达式；
- 不支持递归 CTE、关联子查询、SELECT 投影中的标量子查询、`UNION` 等集合运算、
  `GROUP BY`、窗口函数、`LEFT/RIGHT/FULL/CROSS JOIN`、逗号隐式连接、DML 等，
  遇到即报错指出所在模型并停止分析。

### 传播规则

1. **取值依赖按列引用传递**：输出表达式引用了某列，该列的污染传到这个输出列。
2. **条件污染整表扩散**：字段一旦进入某查询的 `JOIN ON`、`WHERE` 或
   `IN (SELECT ...)` 谓词（含谓词子查询的污染结果集），该查询的**全部输出列**都标记受影响。
3. **跨模型只走真实读取**：下游模型实际 `FROM/JOIN` 读取了上游模型的哪些输出列，
   才接收对应污染；模型可读取上游模型，模型依赖必须是有向无环图。
4. 任一列引用无法解析（未知表/别名、源表或上游模型上不存在的列、未限定列名等）时，
   指出所在模型与具体位置并**停止分析**。

## 目录结构

```
.
├── docker-compose.yml          # 一键启动
└── app/
    ├── Dockerfile
    ├── requirements.txt
    ├── app.py                  # Flask 路由（/、/api/sample、/api/sample_prune、
    │                         #      /api/validate、/api/analyze、/api/prune）
    ├── lineage.py              # SQLGlot 解析、血缘构建、污染传播与路径构造
    ├── prune.py                # 复算裁剪：扁平查询校验、列需求反向传播、SQL 重写与自检
    ├── data/
    │   ├── sample_clearing.json  # 影响分析样例（3 源表 + 3 模型，含 CTE 与多跳链路）
    │   └── sample_prune.json     # 复算裁剪样例（两个分支从同一上游取不同列，汇合为结算模型）
    ├── templates/
    │   └── index.html          # 单页前端（无外部 CDN 依赖，含两个标签页）
    └── tests/
        ├── test_lineage.py     # 传播规则与错误处理测试
        ├── test_prune.py       # 裁剪规则、值/列序/重复行保持与错误处理测试
        └── test_api.py         # Flask 路由与端到端测试
```

## 用 Docker Compose 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>：

**口径影响分析**标签页：

1. 点击「载入清分样例」，或上传自己的 JSON；
2. 在左侧选择源表与待调整字段（如 `ods_card_transaction.transfer_flag`）；
3. 点击「分析受影响模型列」，右侧展示受影响模型、输出列与逐跳依赖路径。

**复算裁剪打包**标签页：

1. 点击「载入裁剪样例」（两个分支从同一明细上游取不同列，汇合为换乘结算模型），或上传 JSON；
2. 选择结算模型（如 `ads_transfer_settlement`），勾选要复算的输出列；
3. 点击「生成精简包」，右侧预览保留/丢弃的模型、各模型保留列与裁剪后 SQL，
   确认后点击「下载精简包 JSON」；写法不受支持或校验失败时只提示原因，不出现下载。

停止：`docker compose down`。

## 输入 JSON 格式

```json
{
  "source_tables": [
    { "table": "ods_card_transaction",
      "columns": ["transaction_id", "card_id", "transfer_flag"] }
  ],
  "models": [
    { "name": "dwd_card_transaction_detail",
      "description": "可选说明",
      "sql": "SELECT t.transaction_id AS transaction_id, t.transfer_flag AS transfer_flag FROM ods_card_transaction t WHERE t.transfer_flag = 0" }
  ]
}
```

- `source_tables[].table` 为源表名，`columns` 为该表全部字段；
- `models[].name` 为模型名，模型 SQL 中 `FROM/JOIN` 同名关系即视为读取该上游模型；
- 其余名字按源表处理，必须在 `source_tables` 中声明；
- 裁剪输出的精简包与输入同格式（`source_tables` 字段清单可能收窄甚至为空，
  `models[].sql` 只保留所需投影列）。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sample` | 返回内置影响分析样例 JSON |
| GET | `/api/sample_prune` | 返回内置复算裁剪样例 JSON（两分支从同一上游取不同列） |
| POST | `/api/validate` | 校验 JSON 与全部 SQL，返回源表字段清单、模型读取关系与各模型输出列 |
| POST | `/api/analyze?table=<源表>&column=<字段>` | 返回受影响模型、输出列与依赖路径 |
| POST | `/api/prune?model=<结算模型>&columns=<逗号分隔输出列>` | 返回精简包（同格式 JSON）与裁剪说明 |

错误统一返回 HTTP 400 与 `{"ok": false, "error": "中文原因（含模型名）"}`。

`/api/prune` 成功时返回：

```json
{
  "ok": true,
  "result": {
    "package": { "source_tables": [], "models": [] },
    "summary": {
      "selected_model": "ads_transfer_settlement",
      "selected_columns": ["settle_amount"],
      "models": [{ "name": "...", "row_only": false,
                   "kept_columns": [], "dropped_columns": [], "sql": "..." }],
      "source_tables": [{ "table": "...", "columns": [] }],
      "dropped_models": [],
      "dropped_source_tables": []
    }
  }
}
```

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin
pip install -r app/requirements.txt
python app/app.py            # 监听 8000
python app/tests/test_lineage.py   # 血缘传播测试
python app/tests/test_prune.py     # 复算裁剪测试
python app/tests/test_api.py       # API 端到端测试
```
