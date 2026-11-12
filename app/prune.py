"""复算裁剪：按选定的结算模型与输出列，把模型包裁成复算所需的最小集合。

历史刷卡流水缺档时，清分工程师用裁剪后的精简包向运营方补取字段：
- 只保留复算链路（选定模型的全部上游）用到的模型、源表与列；
- 同一上游被多个分支读取时，保留各分支用到的列的并集；
- 连接（INNER JOIN ... ON）与筛选（WHERE）及其引用的列原样保留，
  因此所选输出列的值、列序与重复行都与原模型一致；
- 下游只读行、不取任何列时：上游模型保留一个常量列（行数与重复行不变），
  源表的字段清单可以为空。

与血缘分析共用同一个 JSON 格式，但复算裁剪只支持扁平查询：
- 查询仅含 SELECT / FROM / INNER JOIN ... ON / WHERE；
- 投影仅允许带限定名的列或常量（可用 AS 改名）；
- 连接与筛选条件仅允许列或常量之间的比较（=、<>、<、<=、>、>=），可用 AND 连接；
- 不支持 CTE、子查询、DISTINCT 去重、聚合、窗口、集合运算、ORDER BY/LIMIT 等。

全程只解析 SQL、不执行；精简包在返回前会用血缘分析的现有规则再校验一遍，
校验失败会说明原因并拒绝下载。
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from lineage import DIALECT, AnalysisError, build_models

# 只读行不取列时，为上游模型生成的常量列名
ROW_ONLY_COLUMN = "_keep_row"

_COMPARISONS = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)


# ---------------------------------------------------------------------------
# 输入结构校验（口径与 lineage.build_models 一致）
# ---------------------------------------------------------------------------

def _shape_check(payload: dict) -> tuple[list[dict], list[dict]]:
    source_tables = payload.get("source_tables")
    models = payload.get("models")
    if not isinstance(source_tables, list) or not isinstance(models, list):
        raise AnalysisError("JSON 需包含数组字段 source_tables 和 models。")
    seen_tables: set[str] = set()
    for i, tbl in enumerate(source_tables):
        if not isinstance(tbl, dict):
            raise AnalysisError(f"source_tables[{i}] 必须是对象。")
        tname, cols = tbl.get("table"), tbl.get("columns")
        if not tname or not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
            raise AnalysisError(f"source_tables[{i}] 需包含字符串 table 和字符串数组 columns。")
        if tname in seen_tables:
            raise AnalysisError(f"源表「{tname}」在清单中重复出现。")
        seen_tables.add(tname)
    names: list[str] = []
    for m in models:
        if not isinstance(m, dict) or not isinstance(m.get("name"), str) or not m.get("name"):
            raise AnalysisError("models 中每个模型需要唯一的字符串 name。")
        names.append(m["name"])
    if len(set(names)) != len(names):
        raise AnalysisError("models 中每个模型需要唯一的字符串 name。")
    return source_tables, models


def _parse_single(sql: str, model_name: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise AnalysisError(f"模型「{model_name}」SQL 解析失败：{exc}") from exc
    if len(statements) != 1:
        raise AnalysisError(
            f"模型「{model_name}」必须只包含一条 SELECT 语句，当前解析出 {len(statements)} 条。"
        )
    return statements[0]


# ---------------------------------------------------------------------------
# 扁平查询口径校验
# ---------------------------------------------------------------------------

def _unwrap_paren(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _is_constant(node: exp.Expression) -> bool:
    node = _unwrap_paren(node)
    if isinstance(node, (exp.Literal, exp.Boolean, exp.Null)):
        return True
    # 负数字面量视为常量
    return (
        isinstance(node, exp.Neg)
        and isinstance(node.this, exp.Literal)
        and node.this.is_number
    )


def _is_plain_column(node: exp.Expression) -> bool:
    node = _unwrap_paren(node)
    return isinstance(node, exp.Column) and not isinstance(node.this, exp.Star)


def _check_condition(node: exp.Expression, model_name: str, place: str) -> None:
    """连接/筛选条件：仅列或常量的比较，可用 AND 连接。"""
    node = _unwrap_paren(node)
    if isinstance(node, exp.And):
        _check_condition(node.left, model_name, place)
        _check_condition(node.right, model_name, place)
        return
    if isinstance(node, _COMPARISONS):
        for side in (node.left, node.right):
            if not (_is_plain_column(side) or _is_constant(side)):
                raise AnalysisError(
                    f"模型「{model_name}」的{place}条件「{node.sql(DIALECT)}」中，"
                    "比较两侧只能是带限定名的列或常量。"
                )
        return
    raise AnalysisError(
        f"模型「{model_name}」的{place}条件「{node.sql(DIALECT)}」不受支持："
        "连接与筛选只支持列或常量的比较，并可用 AND 连接。"
    )


def _check_projection(proj: exp.Expression, model_name: str) -> None:
    """投影：仅带限定名的列或常量，可用 AS 改名。"""
    node = proj.this if isinstance(proj, exp.Alias) else proj
    node = _unwrap_paren(node)
    if isinstance(node, exp.Column):
        return  # 星号列（t.*）已在结构校验中拦截
    if _is_constant(node):
        return
    if isinstance(node, exp.AggFunc):
        raise AnalysisError(
            f"模型「{model_name}」的投影「{proj.sql(DIALECT)}」包含聚合函数，复算裁剪不支持聚合。"
        )
    raise AnalysisError(
        f"模型「{model_name}」的投影「{proj.sql(DIALECT)}」不受支持："
        "投影只允许带限定名的列或常量（可用 AS 改名）。"
    )


def _check_narrow(tree: exp.Expression, model_name: str) -> None:
    """扁平查询口径：只允许 SELECT / FROM / INNER JOIN ... ON / WHERE。"""
    if isinstance(tree, (exp.Union, exp.Except, exp.Intersect)):
        raise AnalysisError(
            f"模型「{model_name}」包含集合运算，复算裁剪不支持 UNION/EXCEPT/INTERSECT。"
        )
    if not isinstance(tree, exp.Select):
        raise AnalysisError(
            f"模型「{model_name}」只允许单条 SELECT 查询，不支持 {type(tree).__name__}。"
        )
    for node in tree.walk():
        if isinstance(node, exp.With):
            raise AnalysisError(
                f"模型「{model_name}」使用了 CTE（WITH ...），"
                "复算裁剪仅支持扁平 SELECT，不支持 CTE。"
            )
        if isinstance(node, exp.Subquery) or (isinstance(node, exp.Select) and node is not tree):
            raise AnalysisError(
                f"模型「{model_name}」包含子查询，复算裁剪不支持子查询"
                "（含 FROM 派生表与 IN/EXISTS 谓词子查询）。"
            )
        if isinstance(node, (exp.Group, exp.Having)):
            raise AnalysisError(
                f"模型「{model_name}」包含 GROUP BY/HAVING 聚合分组，复算裁剪不支持聚合。"
            )
        if isinstance(node, exp.Window):
            raise AnalysisError(f"模型「{model_name}」包含窗口函数，复算裁剪不支持窗口函数。")
        if isinstance(node, exp.Star):
            raise AnalysisError(
                f"模型「{model_name}」不允许使用星号 SELECT * / t.*，请显式列出列。"
            )
    if tree.args.get("distinct"):
        raise AnalysisError(
            f"模型「{model_name}」使用了 DISTINCT 去重，复算裁剪要求重复行保持不变，不支持去重。"
        )
    if tree.args.get("order"):
        raise AnalysisError(f"模型「{model_name}」使用了 ORDER BY，复算裁剪不支持排序。")
    if tree.args.get("limit") or tree.args.get("offset"):
        raise AnalysisError(f"模型「{model_name}」使用了 LIMIT/OFFSET，复算裁剪不支持。")
    if tree.args.get("qualify"):
        raise AnalysisError(f"模型「{model_name}」使用了 QUALIFY，复算裁剪不支持。")
    if tree.args.get("from_") is None:
        raise AnalysisError(f"模型「{model_name}」缺少 FROM，复算裁剪要求显式读取来源。")
    for join in tree.args.get("joins", []):
        kind = (join.kind or "").upper()
        if kind != "INNER" or join.args.get("side") or join.args.get("method"):
            raise AnalysisError(
                f"模型「{model_name}」仅允许显式 INNER JOIN ... ON ...，发现其他 JOIN 写法。"
            )
        if join.args.get("using"):
            raise AnalysisError(
                f"模型「{model_name}」使用了 JOIN ... USING，请改写为 ON 条件。"
            )
        on = join.args.get("on")
        if on is None:
            raise AnalysisError(f"模型「{model_name}」的 INNER JOIN 缺少 ON 条件。")
        _check_condition(on, model_name, "JOIN ON 连接")
    where = tree.args.get("where")
    if where is not None:
        _check_condition(where.this, model_name, "WHERE 筛选")
    for proj in tree.expressions:
        _check_projection(proj, model_name)


# ---------------------------------------------------------------------------
# 复算链路收集
# ---------------------------------------------------------------------------

def _reachable_models(selected: str, model_sqls: dict[str, str],
                      model_names: set[str]) -> tuple[dict[str, exp.Expression], set[str]]:
    """从结算模型出发，沿 FROM/JOIN 读取关系向上收集可达模型。

    只对复算链路内的模型做扁平查询校验；链路外的模型会被丢弃，不参与校验。
    """
    trees: dict[str, exp.Expression] = {}
    seen: set[str] = set()
    stack = [selected]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        sql = model_sqls.get(name)
        if not isinstance(sql, str) or not sql.strip():
            raise AnalysisError(f"模型「{name}」缺少非空 sql。")
        tree = _parse_single(sql, name)
        _check_narrow(tree, name)
        seen.add(name)
        trees[name] = tree
        for tbl in tree.find_all(exp.Table):
            if tbl.name in model_names and tbl.name not in seen:
                stack.append(tbl.name)
    return trees, seen


# ---------------------------------------------------------------------------
# SQL 裁剪与自检
# ---------------------------------------------------------------------------

def _rewrite_model(tree: exp.Expression, keep: set[str],
                   model_name: str) -> tuple[str, list[str]]:
    """只裁投影列表，FROM/JOIN/WHERE 原样保留。返回（新 SQL, 保留的输出列名）。

    keep 为空表示下游只读行不取列：投影换成一个常量列，行数与重复行不变。
    """
    from_sql = tree.args["from_"].sql(DIALECT)
    joins_sql = [j.sql(DIALECT) for j in tree.args.get("joins", [])]
    where_node = tree.args.get("where")
    where_sql = where_node.sql(DIALECT) if where_node is not None else None
    orig_projs = [(p.alias_or_name, p.sql(DIALECT)) for p in tree.expressions]

    if keep:
        # 按原投影顺序保留所需列，列序不变
        kept = [p for p in tree.expressions if p.alias_or_name in keep]
        if not kept:
            raise AnalysisError(f"内部错误：模型「{model_name}」没有可保留的投影列。")
    else:
        kept = [exp.alias_(exp.Literal.number(1), ROW_ONLY_COLUMN)]
    tree.args["expressions"] = kept
    new_sql = tree.sql(DIALECT)

    # 自检：连接/筛选必须与原文一致，保留列的表达式与列序必须与原文一致
    re = _parse_single(new_sql, model_name)
    re_where_node = re.args.get("where")
    if (
        re.args["from_"].sql(DIALECT) != from_sql
        or [j.sql(DIALECT) for j in re.args.get("joins", [])] != joins_sql
        or (re_where_node.sql(DIALECT) if re_where_node is not None else None) != where_sql
    ):
        raise AnalysisError(
            f"模型「{model_name}」裁剪自检失败：连接或筛选条件发生变化，已阻止下载。"
        )
    if keep:
        expect = [(n, s) for n, s in orig_projs if n in keep]
        got = [(p.alias_or_name, p.sql(DIALECT)) for p in re.expressions]
        if got != expect:
            raise AnalysisError(
                f"模型「{model_name}」裁剪自检失败：保留列的值或列序发生变化，已阻止下载。"
            )
    elif len(re.expressions) != 1 or re.expressions[0].alias_or_name != ROW_ONLY_COLUMN:
        raise AnalysisError(f"模型「{model_name}」裁剪自检失败：常量列生成异常，已阻止下载。")
    return new_sql, [p.alias_or_name for p in re.expressions]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def prune_package(payload: dict, model_name: str, columns: list[str]) -> dict:
    """生成精简包，返回 {"package": 同格式精简 JSON, "summary": 裁剪说明}。"""
    source_decls, model_entries = _shape_check(payload)
    model_sqls = {m["name"]: m.get("sql") for m in model_entries}
    model_names = set(model_sqls)

    if not model_name:
        raise AnalysisError("请先选择要复算的结算模型。")
    if model_name not in model_names:
        raise AnalysisError(f"结算模型「{model_name}」不在 models 中。")
    selected_columns = list(dict.fromkeys(c for c in columns if c))
    if not selected_columns:
        raise AnalysisError("请至少选择一个要复算的输出列。")

    # 1) 复算链路：从结算模型沿读取关系向上，逐个做扁平查询校验
    trees, reachable = _reachable_models(model_name, model_sqls, model_names)

    # 2) 用现有规则校验复算链路（引用解析、列存在性、DAG 等）
    sub_payload = {
        "source_tables": source_decls,
        "models": [m for m in model_entries if m["name"] in reachable],
    }
    source_schemas, infos, order = build_models(sub_payload)

    root_outputs = infos[model_name].nodes[-1].output_columns
    for col in selected_columns:
        if col not in root_outputs:
            raise AnalysisError(
                f"模型「{model_name}」没有输出列「{col}」，"
                f"可选输出列：{', '.join(root_outputs)}。"
            )

    # 3) 列需求反向传播：保留列的投影 + 连接/筛选引用的列；多分支读取取并集
    required: dict[str, set[str]] = {name: set() for name in infos}
    required[model_name] = set(selected_columns)
    table_cols: dict[str, set[str]] = {t: set() for t in source_schemas}
    for name in reversed(order):  # order 为上游在前，逆序即下游先处理
        node = infos[name].nodes[-1]  # 扁平查询只有根查询一个节点
        leaves = set(node.predicate_leaves)  # 连接与筛选引用的列全部保留
        for out_col in required[name]:
            leaves |= node.output_leaves[out_col]
        for leaf in leaves:
            kind, actual = node.sources[leaf.relation]
            if kind == "model":
                required[actual].add(leaf.column)
            else:
                table_cols[actual].add(leaf.column)
    referenced_tables: set[str] = set()
    for info in infos.values():
        for node in info.nodes:
            for kind, actual in node.sources.values():
                if kind == "table":
                    referenced_tables.add(actual)

    # 4) 重写链路内各模型 SQL：只裁投影，连接与筛选原样保留
    rewritten: dict[str, tuple[str, list[str]]] = {}
    for name in order:
        rewritten[name] = _rewrite_model(trees[name], required[name], name)

    slimmed_models = []
    for m in model_entries:
        name = m["name"]
        if name not in reachable:
            continue
        entry = {"name": name}
        if m.get("description"):
            entry["description"] = m["description"]
        entry["sql"] = rewritten[name][0]
        slimmed_models.append(entry)

    slimmed_tables = [
        {
            "table": t["table"],
            # 只保留被引用的列，原字段顺序不变；只读行的源表字段清单为空
            "columns": [c for c in source_schemas[t["table"]] if c in table_cols[t["table"]]],
        }
        for t in source_decls
        if t["table"] in referenced_tables
    ]

    package = {k: v for k, v in payload.items() if k not in ("source_tables", "models")}
    package["source_tables"] = slimmed_tables
    package["models"] = slimmed_models

    # 5) 下载前用现有规则校验精简包
    try:
        build_models(package)
    except AnalysisError as exc:
        raise AnalysisError(f"精简包未通过现有规则校验：{exc}（已阻止下载）。") from exc

    summary = {
        "selected_model": model_name,
        "selected_columns": [c for c in root_outputs if c in required[model_name]],
        "models": [
            {
                "name": name,
                "row_only": not required[name],
                "kept_columns": rewritten[name][1],
                "dropped_columns": [
                    c for c in infos[name].nodes[-1].output_columns
                    if c not in set(rewritten[name][1])
                ],
                "sql": rewritten[name][0],
            }
            for name in order
        ],
        "source_tables": slimmed_tables,
        "dropped_models": [m["name"] for m in model_entries if m["name"] not in reachable],
        "dropped_source_tables": [
            t["table"] for t in source_decls if t["table"] not in referenced_tables
        ],
    }
    return {"package": package, "summary": summary}
