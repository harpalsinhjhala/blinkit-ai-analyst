"""Builds the MENU the AI may use, for ANY Power BI semantic model.

Two sources, combined:
1. Auto-discovery: asks the model for its own measures and columns (DAX INFO.VIEW functions).
2. Optional rules file (MODEL_RULES=rules/<name>.yaml): picks the exact menu, adds business
   meanings, default filters and warnings. Anything the rules file leaves out is auto-discovered.

Point the app at a new model (WORKSPACE_ID, SEMANTIC_MODEL_ID, REPORT_ID) and it works with no code changes.
"""
import os
import re

import yaml

from pbi_client import get_model_name, run_dax

HERE = os.path.dirname(os.path.abspath(__file__))

# Helper measures that only drive visuals (colours, arrows, titles): not useful to the AI
NOISE = re.compile(r"colou?r|arrow|indicator|icon|title|label|tooltip|format|selected|dynamic|bookmark|"
                   r"button|nav|header|subtitle|placeholder|blank|dummy|spacer|^_|sort", re.I)
# INFO.VIEW reports types as Integer / Number / Text / Date / True/False
NUMERIC_TYPES = {"Integer", "Number", "Int64", "Double", "Decimal", "Currency"}
PERIODISH = re.compile(r"year|month|quarter|week|day|period|age|fy\b", re.I)
KEYISH = re.compile(r"(\bid\b|_id$|id$|key$|_sk$|guid|lineage)", re.I)
SKIP_TABLES = re.compile(r"^(LocalDateTable|DateTableTemplate)", re.I)
PROMPT_CAP = 60  # max measures / fields listed in the AI prompt; the rest are found with describe_model(search)


def quote_table(name):
    return "'" + name.replace("'", "''") + "'"


def col_ref(table, column):
    return f"{quote_table(table)}[{column.replace(']', ']]')}]"


def measure_ref(name):
    return f"[{name.replace(']', ']]')}]"


def _split_ref(ref):
    """'Table'[Column] or Table[Column] -> (table, column)."""
    m = re.match(r"^'?(.+?)'?\[(.+)\]$", ref.strip())
    if not m:
        raise ValueError(f"Bad column reference '{ref}'. Use Table[Column].")
    return m.group(1).replace("''", "'"), m.group(2).replace("]]", "]")


COUNTISH = re.compile(r"count|qty|quantity|units|number of|\bno\b|#|orders|customers|members", re.I)


def _auto_format(fmt, dtype, name=""):
    fs = (fmt or "").lower()
    if "%" in fs:
        return "pct"
    if dtype in ("Integer", "Int64") and COUNTISH.search(name):
        return "count"
    return "auto"  # K / M / B scaling


def _discover():
    measures = run_dax("EVALUATE SELECTCOLUMNS(FILTER(INFO.VIEW.MEASURES(), NOT [IsHidden]), "
                       '"name", [Name], "table", [Table], "desc", [Description], "type", [DataType], '
                       '"fmt", [FormatString], "folder", [DisplayFolder])')
    columns = run_dax("EVALUATE SELECTCOLUMNS(FILTER(INFO.VIEW.COLUMNS(), NOT [IsHidden]), "
                      '"name", [Name], "table", [Table], "desc", [Description], "type", [DataType], '
                      '"key", [IsKey], "sum", [SummarizeBy], "fmt", [FormatString])')
    tables = run_dax('EVALUATE SELECTCOLUMNS(FILTER(INFO.VIEW.TABLES(), NOT [IsHidden]), "name", [Name])')
    rels = run_dax('EVALUATE SELECTCOLUMNS(INFO.VIEW.RELATIONSHIPS(), "t", [FromTable], "c", [FromColumn])')
    join_cols = {(r["t"], r["c"]) for r in rels}  # "many"-side keys: duplicates of a lookup table's key
    return measures, columns, [t["name"] for t in tables], join_cols


def _auto_measures(raw_measures, raw_columns, join_cols):
    out = {}
    for m in sorted(raw_measures, key=lambda m: m["name"]):
        if m["type"] not in NUMERIC_TYPES or NOISE.search(m["name"]):
            continue
        out[m["name"]] = {"ref": measure_ref(m["name"]), "format": _auto_format(m["fmt"], m["type"], m["name"]),
                          "description": m["desc"] or "", "folder": m["folder"] or ""}
    # Implicit measures: numeric columns Power BI would sum/count when dragged into a visual
    for c in raw_columns:
        if (c["type"] not in NUMERIC_TYPES or c["sum"] in (None, "None") or SKIP_TABLES.search(c["table"])
                or (c["table"], c["name"]) in join_cols or KEYISH.search(c["name"]) or PERIODISH.search(c["name"])):
            continue
        agg = {"Count": "COUNT", "Average": "AVERAGE", "Min": "MIN", "Max": "MAX",
               "DistinctCount": "DISTINCTCOUNT"}.get(c["sum"], "SUM")
        label = {"COUNT": "Count of", "AVERAGE": "Average", "MIN": "Min", "MAX": "Max",
                 "DISTINCTCOUNT": "Distinct count of"}.get(agg, "Total")
        name = f"{label} {c['name'].strip()}"
        if name in out:
            name = f"{label} {c['table']} {c['name'].strip()}"
        out[name] = {"ref": f"{agg}({col_ref(c['table'], c['name'])})",
                     "format": "count" if agg in ("COUNT", "DISTINCTCOUNT") else _auto_format(c["fmt"], c["type"], c["name"]),
                     "description": c["desc"] or f"{label} of column {c['table']}[{c['name']}] (implicit measure)",
                     "folder": ""}
    return out


def _auto_fields(raw, join_cols):
    picked = []
    for c in raw:
        if (SKIP_TABLES.search(c["table"]) or c["key"] or KEYISH.search(c["name"])
                or (c["table"], c["name"]) in join_cols):
            continue
        if c["type"] in ("Number", "Double", "Decimal", "Currency", "Date", "DateTime", "Binary"):
            continue  # amounts and timestamps are values, not slice-by fields
        if c["type"] in ("Integer", "Int64") and not PERIODISH.search(c["name"]):
            continue  # whole-number amounts (qty, sales) are values; years/months are fields
        picked.append(c)
    names = {}
    for c in picked:
        names.setdefault(c["name"].strip(), []).append(c)
    out = {}
    for name, cols in names.items():
        for c in cols:
            friendly = name if len(cols) == 1 else f"{c['table']} {name}"
            out[friendly] = {"column": col_ref(c["table"], c["name"]), "numeric": c["type"] in NUMERIC_TYPES,
                             "note": c["desc"] or ""}
    return dict(sorted(out.items()))


def _find_rules(model_name):
    """MODEL_RULES=path picks a rules file; MODEL_RULES=auto forces auto mode; unset = use the file in rules/
    whose 'model:' matches the connected model's name (so dropping a rules file in rules/ is enough)."""
    choice = os.getenv("MODEL_RULES", "").strip()
    if choice.lower() in ("auto", "none"):
        return None
    if choice:
        return os.path.join(HERE, choice)
    folder = os.path.join(HERE, "rules")
    for fn in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if fn.endswith((".yaml", ".yml")):
            with open(os.path.join(folder, fn), encoding="utf-8") as f:
                head = yaml.safe_load(f) or {}
            if str(head.get("model", "")).strip().lower() == model_name.strip().lower():
                return os.path.join(folder, fn)
    return None


def load_profile():
    try:
        model_name = get_model_name()
    except Exception:
        model_name = "Power BI model"
    rules_path = _find_rules(model_name)
    rules = {}
    if rules_path:
        with open(rules_path, encoding="utf-8") as f:
            rules = yaml.safe_load(f) or {}

    raw_measures, raw_columns, tables, join_cols = _discover()
    col_types = {(c["table"], c["name"]): c["type"] for c in raw_columns}

    # Measures: from rules if listed, else auto
    if isinstance(rules.get("measures"), dict):
        known = {m["name"] for m in raw_measures}
        measures = {}
        for name, spec in rules["measures"].items():
            spec = spec or {}
            real = spec.get("measure", name)
            if real not in known:
                raise ValueError(f"Rules file: measure '{real}' is not in the model.")
            measures[name] = {"ref": measure_ref(real), "format": spec.get("format", "auto"),
                              "description": spec.get("description", ""), "folder": ""}
    else:
        measures = _auto_measures(raw_measures, raw_columns, join_cols)

    # Fields: from rules if listed, else auto
    if isinstance(rules.get("fields"), dict):
        fields = {}
        for name, spec in rules["fields"].items():
            spec = spec if isinstance(spec, dict) else {"column": spec}
            t, c = _split_ref(spec["column"])
            if (t, c) not in col_types:
                raise ValueError(f"Rules file: column {spec['column']} is not in the model.")
            fields[name] = {"column": col_ref(t, c), "numeric": col_types[(t, c)] in NUMERIC_TYPES,
                            "note": spec.get("note", "")}
    else:
        fields = _auto_fields(raw_columns, join_cols)

    context = []
    for cx in rules.get("context") or []:
        t, c = _split_ref(cx["column"])
        context.append({**cx, "column": col_ref(t, c), "numeric": col_types.get((t, c)) in NUMERIC_TYPES})

    title = rules.get("title") or model_name
    m_names, f_names = list(measures), list(fields)
    suggestions = rules.get("suggestions") or [
        s for s in [
            f"What is the total {m_names[0]}?" if m_names else None,
            f"Show {m_names[0]} by {f_names[0]}, highest first" if m_names and f_names else None,
            f"Chart: {m_names[min(1, len(m_names) - 1)]} by {f_names[min(1, len(f_names) - 1)]}"
            if m_names and f_names else None,
            "What can I ask about this data?",
        ] if s
    ]
    return {
        "model_name": model_name,
        "title": title,
        "assistant": rules.get("assistant") or f"an analytics assistant for the Power BI model '{model_name}'",
        "greeting": rules.get("greeting") or (
            f"Hi! I can answer questions about the {title} data, e.g. "
            + ", ".join(m_names[:4]) + ". Ask for a chart too. I query the same model as the report on the left."),
        "suggestions": suggestions,
        "mode": "rules file + model" if rules else "auto (read from the model)",
        "measures": measures,
        "fields": fields,
        "context": context,
        "warnings": rules.get("warnings") or [],
        "prompt_notes": rules.get("prompt_notes") or [],
        "fallback_tables": rules.get("fallback_tables") or [t for t in tables if not re.match(
            r"^(LocalDateTable|DateTableTemplate)", t)],
        "run_dax_notes": rules.get("run_dax_notes") or (
            "Use SUMMARIZECOLUMNS with columns and plain aggregations (SUM, COUNTROWS, AVERAGE, DISTINCTCOUNT), "
            "or measures in [brackets]. Example: EVALUATE SUMMARIZECOLUMNS('Date'[Year], \"Sales\", [Total Sales])"),
    }
