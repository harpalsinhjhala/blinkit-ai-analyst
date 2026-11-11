"""Power BI MCP server: the "kitchen". Works with ANY published semantic model.

At startup it reads the model's menu (model_profile.py: auto-discovery + optional rules file).
The AI never writes DAX for normal questions: it picks measures, slice-by fields and filters from
the menu, and this server builds the DAX and runs it on the published Power BI model.

Tools:      describe_model, list_values, get_measures (+ fallbacks describe_table, run_dax)
Prompt:     analyst_instructions  (system prompt for any MCP client, built from the model)
Resource:   model://ui            (title, greeting, suggestions for the web page)

Run:  .venv/Scripts/python mcp_server.py      (speaks MCP over stdio)
"""
import json
from datetime import date

from mcp.server.fastmcp import FastMCP

from model_profile import PROMPT_CAP, load_profile, quote_table
from pbi_client import run_dax as run_dax_query

P = load_profile()
MEASURES, FIELDS, CONTEXT = P["measures"], P["fields"], P["context"]
MAX_ROWS = 50

mcp = FastMCP("jhala-ai-powerbi")


# ---------- helpers ----------
def _match(name, options, kind):
    """Case-insensitive match of a name the AI sent against the menu."""
    n = str(name).strip().lower()
    for key in options:
        if key.lower() == n:
            return key
    close = [k for k in options if n in k.lower()]
    if len(close) == 1:
        return close[0]
    hint = ", ".join(close[:15]) if close else ", ".join(list(options)[:40])
    raise ValueError(f"Unknown {kind} '{name}'. Use describe_model to search. Closest {kind}s: {hint}")


def _dax_str(v):
    return '"' + str(v).replace('"', '""') + '"'


def _dax_value(numeric, v):
    return str(int(float(v))) if numeric else _dax_str(v)


def _fmt(value, kind):
    if value is None:
        return "-"
    if not isinstance(value, (int, float)):
        return str(value)
    if kind == "pct":
        return f"{value:.2%}"
    if kind == "count":
        return f"{value:,.0f}"
    if kind == "inr":  # Indian rupees: crore / lakh, like the report
        if abs(value) >= 1e7:
            return f"₹{value / 1e7:.2f} Cr"
        if abs(value) >= 1e5:
            return f"₹{value / 1e5:.2f} L"
        return f"₹{value:,.0f}" if abs(value) >= 100 else f"₹{value:,.2f}"
    if kind == "ratio":
        return f"{value:.2f}x"
    if kind == "decimal":
        return f"{value:,.2f}"
    a = abs(value)
    if kind == "money":
        return f"{value / 1e9:.2f}B" if a >= 1e9 else f"{value / 1e6:.2f}M"
    if a >= 1e9:
        return f"{value / 1e9:.2f}B"
    if a >= 1e6:
        return f"{value / 1e6:.2f}M"
    if a >= 1e4:
        return f"{value / 1e3:.1f}K"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


_values_cache = {}


def _members(field):
    if field not in _values_cache:
        col = FIELDS[field]["column"]
        rows = run_dax_query(f"EVALUATE VALUES({col})")
        _values_cache[field] = [str(list(r.values())[0]) for r in rows if list(r.values())[0] not in (None, "")]
    return _values_cache[field]


def _resolve_values(field, values):
    """Map what the AI typed to real members: exact (any case) > unique partial match > error with the list.
    Saves the AI from silently getting 0 rows for e.g. "Dairy" instead of "Dairy & Breakfast"."""
    if FIELDS[field]["numeric"]:
        return values
    members = _members(field)
    out = []
    for v in values:
        vl = str(v).strip().lower()
        exact = [m for m in members if m.lower() == vl]
        part = [m for m in members if vl in m.lower()]
        if exact:
            out.append(exact[0])
        elif len(part) == 1:
            out.append(part[0])
        else:
            raise ValueError(f"'{v}' is not a {field} value. Valid values: {', '.join(members[:60])}")
    return out


def _context_filter(cx, value):
    if cx["kind"] == "date_upto":
        d = date.fromisoformat(str(value))
        return (f"DATESBETWEEN({cx['column']}, DATE(1900,1,1), DATE({d.year},{d.month},{d.day}))",
                d.isoformat())
    if cx["kind"] == "equals":
        return f"TREATAS({{{_dax_value(cx['numeric'], value)}}}, {cx['column']})", value
    if cx["kind"] == "dax":
        return cx["dax"].replace("{value}", str(value)), value
    raise ValueError(f"Unknown context kind {cx['kind']}")


def _context_doc():
    if not CONTEXT:
        return ""
    parts = [f"'{c['name']}' (default {c['default']})" for c in CONTEXT]
    return ("\n    context:    optional {name: value} to change the default filters " + ", ".join(parts)
            + ". Dates as YYYY-MM-DD.")


# ---------- tools ----------
@mcp.tool()
def describe_model(search: str | None = None) -> dict:
    """List the measures (numbers you can ask for) and fields (to slice or filter by) on the menu,
    with their meaning and the default filters. Pass search="sales" to find matching names only."""
    s = (search or "").strip().lower()
    hit = (lambda n, d: not s or s in n.lower() or s in (d or "").lower())
    ms = {n: m["description"] for n, m in MEASURES.items() if hit(n, m["description"])}
    fs = {n: f["note"] for n, f in FIELDS.items() if hit(n, f["note"])}
    return {
        "model": P["model_name"],
        "measures": dict(list(ms.items())[:150]),
        "fields": dict(list(fs.items())[:150]),
        "default_filters": {c["name"]: c["default"] for c in CONTEXT},
        "counts": {"measures": len(ms), "fields": len(fs)},
    }


@mcp.tool()
def list_values(field: str) -> dict:
    """List the exact values of one field (e.g. all region names), to use in filters."""
    f = _match(field, FIELDS, "field")
    col = FIELDS[f]["column"]
    rows = run_dax_query(f"EVALUATE VALUES({col}) ORDER BY {col}")
    values = [list(r.values())[0] for r in rows if list(r.values())[0] not in (None, "")]
    return {"field": f, "values": values[:200], "total": len(values)}


def get_measures(
    measures: list[str],
    group_by: list[str] | None = None,
    filters: dict[str, list[str]] | None = None,
    context: dict[str, str] | None = None,
    top_n: int | None = None,
    sort_desc: bool = True,
) -> dict:
    group_by, filters, context = group_by or [], filters or {}, context or {}
    m_names = [_match(m, MEASURES, "measure") for m in measures]
    dims = [_match(g, FIELDS, "field") for g in group_by]
    flt = {}
    for d, v in filters.items():
        f = _match(d, FIELDS, "field")
        flt[f] = _resolve_values(f, v if isinstance(v, list) else [v])

    filter_args, applied = [], {}
    given = {str(k).strip().lower(): v for k, v in context.items()}
    for cx in CONTEXT:
        skip = cx.get("skip_when_field")
        if skip and (skip in dims or skip in flt):
            continue
        expr, shown = _context_filter(cx, given.get(cx["name"].lower(), cx["default"]))
        filter_args.append(expr)
        applied[cx["name"]] = shown
    for f, values in flt.items():
        col = FIELDS[f]["column"]
        filter_args.append(f"TREATAS({{{', '.join(_dax_value(FIELDS[f]['numeric'], v) for v in values)}}}, {col})")
        applied[f] = values

    group_cols = [FIELDS[d]["column"] for d in dims]
    measure_args = [f'{_dax_str(m)}, {MEASURES[m]["ref"]}' for m in m_names]
    table = "SUMMARIZECOLUMNS(" + ", ".join(group_cols + measure_args) + ")"
    dax = "EVALUATE " + (f"CALCULATETABLE({table}, {', '.join(filter_args)})" if filter_args else table)
    rows = run_dax_query(dax)

    # Drop rows where every measure is 0/blank (cross-join noise), sort, cut
    rows = [r for r in rows if any((r.get(m) or 0) != 0 for m in m_names)]
    rows.sort(key=lambda r: r.get(m_names[0]) or 0, reverse=sort_desc)
    if top_n:
        rows = rows[: int(top_n)]
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]

    out_rows = []
    for r in rows:
        row = {}
        low = {k.lower(): v for k, v in r.items()}  # Power BI may return column names in another case
        for d, col in zip(dims, group_cols):
            row[d] = low.get(col.split("[")[-1].rstrip("]").replace("]]", "]").lower())
        for m in m_names:
            row[m] = _fmt(r.get(m), MEASURES[m]["format"])
            row[m + " (raw)"] = r.get(m)
        out_rows.append(row)

    warnings = []
    for w in P["warnings"]:
        hit_m = [m for m in m_names if m in w.get("measures", [])]
        for d in dims + list(flt):
            if d in w.get("fields", []) and hit_m:
                warnings.append(w["message"].replace("{field}", d).replace("{measures}", ", ".join(hit_m)))
    # Highest / lowest worked out here, so the AI never has to compare numbers itself
    summary = {}
    if dims and len(out_rows) > 1:
        m0 = m_names[0]
        valued = [r for r in out_rows if isinstance(r.get(m0 + " (raw)"), (int, float))]
        if valued:
            label = lambda r: " / ".join(str(r[d]) for d in dims)
            hi = max(valued, key=lambda r: r[m0 + " (raw)"])
            lo = min(valued, key=lambda r: r[m0 + " (raw)"])
            summary = {f"highest {m0}": f"{label(hi)} ({hi[m0]})", f"lowest {m0}": f"{label(lo)} ({lo[m0]})"}
    return {"filters_applied": applied, "summary": summary, "rows": out_rows, "row_count": len(out_rows),
            "truncated": truncated, "warnings": warnings}


mcp.tool(description=(
    f"Get one or more measures from the '{P['model_name']}' model, optionally split and filtered.\n"
    "    measures:   names from the menu, e.g. [\"" + "\", \"".join(list(MEASURES)[:2]) + "\"]\n"
    "    group_by:   optional field names to split by, e.g. [\"" + (list(FIELDS)[0] if FIELDS else "") + "\"]\n"
    "    filters:    optional {field: [values]}; use list_values for exact values"
    + _context_doc() + "\n"
    "    top_n:      return only the top N rows, sorted by the first measure\n"
    "    sort_desc:  True = highest first"))(get_measures)


# ---------- fallback: custom DAX (only when get_measures can't answer) ----------
TABLES = P["fallback_tables"]


@mcp.tool(description="FALLBACK ONLY. List the visible columns of one table, to write a run_dax query. "
                      "Tables: " + ", ".join(TABLES[:60]))
def describe_table(table: str) -> dict:
    t = _match(table, TABLES, "table")
    rows = run_dax_query(
        "EVALUATE SELECTCOLUMNS(FILTER(INFO.VIEW.COLUMNS(), [Table] = " + _dax_str(t)
        + ' && NOT [IsHidden]), "column", [Name], "type", [DataType])'
    )
    return {"table": t, "dax_name": quote_table(t), "columns": {r["column"]: r["type"] for r in rows}}


@mcp.tool(description="FALLBACK ONLY: use when get_measures cannot answer (e.g. monthly trends, raw columns "
                      "not on the menu). Runs a read-only DAX query that starts with EVALUATE. "
                      + P["run_dax_notes"] + " Results are NOT validated report measures: say so in the answer.")
def run_dax(query: str) -> dict:
    q = query.strip()
    if not q.upper().startswith(("EVALUATE", "DEFINE")):
        raise ValueError("Query must start with EVALUATE (or DEFINE ... EVALUATE).")
    rows = run_dax_query(q)
    clean = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in rows[:MAX_ROWS]]
    return {"rows": clean, "row_count": len(clean), "truncated": len(rows) > MAX_ROWS,
            "note": "Custom query on raw columns, not a validated report measure."}


# ---------- prompt + resource for MCP clients ----------
@mcp.prompt()
def analyst_instructions() -> str:
    """System prompt for an AI analyst on this model."""
    ms = list(MEASURES)
    fs = [f"{n} ({FIELDS[n]['note']})" if FIELDS[n]["note"] else n for n in FIELDS]
    more_m = f" (+{len(ms) - PROMPT_CAP} more: use describe_model(search=...))" if len(ms) > PROMPT_CAP else ""
    more_f = f" (+{len(fs) - PROMPT_CAP} more: use describe_model(search=...))" if len(fs) > PROMPT_CAP else ""
    ctx = ""
    if CONTEXT:
        ctx = ("- Unless the user says otherwise, get_measures applies these default filters: "
               + ", ".join(f"{c['name']} = {c['default']}" for c in CONTEXT)
               + ". To change one, pass context={name: value}. In get_measures answers, state the "
               "filters_applied values (never in run_dax answers).\n")
    notes = "".join(f"- {n}\n" for n in P["prompt_notes"])
    return (
        f"You are {P['assistant']}.\nRules:\n"
        "- Get every number from the tools. Never guess or invent a figure.\n"
        f"- Measures available: {', '.join(ms[:PROMPT_CAP])}{more_m}.\n"
        f"- Fields to split or filter by: {', '.join(fs[:PROMPT_CAP])}{more_f}.\n"
        "  Use these exact names with get_measures. Use list_values to get exact values before filtering.\n"
        + ctx +
        "- Show numbers exactly as the tool formats them (e.g. 11.02M, 86.57%). Use a short markdown table "
        "for lists of 3+ rows.\n"
        "- Do not do your own arithmetic (shares, percentages, sums, differences). Never write claims like "
        "'more than X and Y combined', 'about 1M more' or 'twice as much': you cannot add or subtract reliably. "
        "Compare only with words like highest, lowest, similar, followed by.\n"
        "- Do not guess business reasons; only state what the numbers show.\n"
        "- For highest / lowest / peak, use the 'summary' in the get_measures result word for word. Never pick "
        "them yourself from the rows.\n"
        "- If a tool returns warnings, mention them briefly.\n"
        "- For charts: first get the data (get_measures or run_dax), then CALL the show_chart tool. Never write "
        "chart JSON in your text. After the chart, still give a short text answer.\n"
        "- Use run_dax only when get_measures cannot answer (e.g. monthly trends). Call describe_table first if "
        "unsure of column names.\n"
        + notes +
        "- If the user asks what they can ask, summarise the measures and fields briefly.\n"
        "- If the data cannot answer the question (forecasts, reasons why, data not in the model), say so.\n"
        "- Be concise: answer first, then one line of insight if useful."
    )


@mcp.resource("model://ui")
def ui_info() -> str:
    """Title, greeting and suggested questions for a chat page."""
    return json.dumps({"title": P["title"], "greeting": P["greeting"], "suggestions": P["suggestions"],
                       "mode": P["mode"], "model": P["model_name"],
                       "defaults": {c["name"]: c["default"] for c in CONTEXT},
                       "measures": len(MEASURES), "fields": len(FIELDS)})


if __name__ == "__main__":
    mcp.run()  # stdio
