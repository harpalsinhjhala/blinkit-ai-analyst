"""Jhala AI web app (by Harpal Sinh Jhala): the "dining room" + "waiter".

- Serves one page: the published Power BI report (left) + a chat panel (right).
- The chat sends questions to a free LLM on Groq.
- The LLM answers by calling OUR MCP server's tools (mcp_server.py) over the MCP protocol.
  The app is an MCP client, exactly like Claude Desktop or Cowork would be.
- Nothing here is model-specific: the system prompt, title and suggestions come from the MCP
  server, which reads them from whichever Power BI model the IDs point to.

Run:  .venv/Scripts/python app.py      then open http://localhost:8000
"""
import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from html import escape

import requests
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_TOOL_ROUNDS = 8

state = {}


@asynccontextmanager
async def lifespan(app):
    # Start our MCP server once and keep one MCP session open
    stack = AsyncExitStack()
    # env=os.environ: the MCP SDK otherwise passes only PATH/HOME etc., and the server would miss
    # the host's settings (Render / Codespaces have no .env file)
    server = StdioServerParameters(command=sys.executable, args=[os.path.join(HERE, "mcp_server.py")],
                                   cwd=HERE, env=dict(os.environ))
    read, write = await stack.enter_async_context(stdio_client(server))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    tools = (await session.list_tools()).tools
    state["session"] = session
    state["tools"] = [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.inputSchema}}
        for t in tools
    ]
    # The model-specific parts come from the MCP server: its prompt and its UI resource
    prompt = await session.get_prompt("analyst_instructions")
    state["system_prompt"] = prompt.messages[0].content.text
    ui = await session.read_resource("model://ui")
    state["ui"] = json.loads(ui.contents[0].text)
    yield
    await stack.aclose()


app = FastAPI(lifespan=lifespan)


class ChatRequest(BaseModel):
    messages: list[dict]  # [{"role": "user"|"assistant", "content": "..."}]


def _call_llm(messages):
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is empty in .env. Get a free key at console.groq.com and restart the app.")
    # Each Groq model has its own free daily quota: when the main one runs out, use the fallback model
    models = [m for m in [os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"), os.getenv("GROQ_FALLBACK_MODEL")] if m]
    if state.get("main_model_out_until", 0) > time.time() and len(models) > 1:
        models = models[1:]
    model = models[0]
    bad_tool_calls = 0
    for attempt in range(7):
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": messages,
                "tools": state["tools"] + [CHART_TOOL],
                "tool_choice": "auto",
                "temperature": 0,
            },
            timeout=60,
        )
        # The model produced a tool call that doesn't match the tool's schema: sampling differs per call,
        # so ask again (up to 2 times) instead of showing the visitor a raw API error.
        if r.status_code == 400 and "tool_use_failed" in r.text and bad_tool_calls < 2:
            bad_tool_calls += 1
            continue
        if r.status_code != 429:
            break
        if "per day" in r.text and model != models[-1]:  # main model's daily quota used up -> fallback model
            state["main_model_out_until"] = time.time() + 15 * 60
            model = models[-1]
            continue
        if "per day" in r.text:  # daily free quota used up: waiting won't help, say so plainly
            m = re.search(r"try again in ([\dhms.]+)", r.text)
            wait = f" Try again in about {m.group(1).split('.')[0]}s." if m else " Try again later."
            raise RuntimeError("The free AI quota for today is used up (Groq free tier: 200k tokens/day)." + wait)
        # Free tier = 8k tokens/minute. Wait as long as Groq asks, then retry.
        m = re.search(r"try again in ([\d.]+)s", r.text)
        time.sleep(min(float(m.group(1)) + 0.5 if m else 10, 30))
    if not r.ok:
        raise RuntimeError(f"LLM error {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]


# Chart tool: lives in the app (the page draws it), not in the MCP server (which only serves data).
# The LLM picks the chart type and columns; the numbers come from the last data result, never from the LLM.
CHART_TOOL = {"type": "function", "function": {
    "name": "show_chart",
    "description": "Draw a chart in the chat from the rows of the most recent get_measures or run_dax result. "
                   "Use when the user asks for a chart, graph, visual or trend. You only choose the type and "
                   "columns; the data is filled in automatically. Use line for trends over years or months, "
                   "pie only for one measure split into parts. For one colour per value of a second field "
                   "(e.g. 'different colour for each Year'), first get the data grouped by BOTH fields, "
                   "then set color_by to the second field (and stacked=true for a stacked bar). "
                   "This tool is the ONLY way to show a chart: never write chart settings or JSON in your answer.",
    "parameters": {"type": "object", "properties": {
        "chart_type": {"type": "string", "enum": ["bar", "line", "pie"]},
        "x": {"type": "string", "description": "Column for the categories / x-axis, e.g. a field name"},
        "y": {"type": "array", "items": {"type": "string"}, "description": "Measure / value columns to plot"},
        # Optional fields also accept null: models often send null for "not used", and Groq rejects the
        # whole call (400 tool_use_failed) when that doesn't match the schema.
        "color_by": {"type": ["string", "null"], "description": "Optional second field: one coloured series per "
                                                              "value (e.g. 'Year'). Use with exactly one y column."},
        "stacked": {"type": ["boolean", "null"], "description": "Bar charts with color_by: stack the colours in one "
                                                              "bar (true) or put them side by side (false, default)."},
        "title": {"type": "string"},
    }, "required": ["chart_type", "x", "y", "title"]},
}}

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _order_key(v):
    """Years and months read left to right; raises ValueError for other text."""
    v = str(v).strip()
    m = re.match(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{4})$", v)  # "Mar 2024" -> year, then month
    if m and m.group(1).lower() in MONTHS:
        return int(m.group(2)) * 12 + MONTHS.index(m.group(1).lower())
    if v.lower()[:3] in MONTHS:
        return MONTHS.index(v.lower()[:3])
    if re.match(r"^Q[1-4]$", v, re.I):
        return int(v[1])
    return float(v)


def _build_chart(args, rows):
    if not rows:
        raise ValueError("No data yet: call get_measures or run_dax first, then show_chart.")
    cols = [c for c in rows[0] if not c.endswith("(raw)")]
    x, ys = args["x"], args["y"] if isinstance(args["y"], list) else [args["y"]]
    color_by = args.get("color_by") or None
    for c in [x] + ys + ([color_by] if color_by else []):
        if c not in cols:
            raise ValueError(f"Column '{c}' is not in the last result. Columns: {', '.join(cols)}. "
                             "Get the data grouped by these fields first.")
    val = lambda r, y: r.get(y + " (raw)", r.get(y))
    pct = lambda y: str(rows[0].get(y, "")).endswith("%")
    inr = lambda y: str(rows[0].get(y, "")).startswith("₹")
    base = {"type": args["chart_type"], "title": args.get("title", ""), "stacked": bool(args.get("stacked"))}

    if color_by:
        if len(ys) != 1:
            raise ValueError("color_by works with exactly one y column.")
        y = ys[0]
        # x categories: years/months in order, otherwise biggest total first
        totals = {}
        for r in rows:
            totals[str(r[x])] = totals.get(str(r[x]), 0) + (val(r, y) or 0)
        try:
            xs = sorted(totals, key=_order_key)
        except ValueError:
            xs = sorted(totals, key=lambda k: totals[k], reverse=True)
        groups = list(dict.fromkeys(str(r[color_by]) for r in rows))
        try:
            groups.sort(key=_order_key)
        except ValueError:
            pass
        cell = {(str(r[x]), str(r[color_by])): val(r, y) for r in rows}
        series = [{"name": g, "values": [cell.get((k, g)) for k in xs], "pct": pct(y), "inr": inr(y)} for g in groups]
        return {**base, "x": xs, "series": series, "legend_title": color_by, "y_title": y}

    rows = list(rows)
    try:
        rows.sort(key=lambda r: _order_key(r[x]))
    except ValueError:
        pass  # other categories keep the query's order (biggest first)
    series = [{"name": y, "values": [val(r, y) for r in rows], "pct": pct(y), "inr": inr(y)} for y in ys]
    return {**base, "x": [str(r[x]) for r in rows], "series": series}


def _slim(tool_text):
    """Drop the '(raw)' numbers before sending tool results to the LLM (saves tokens)."""
    try:
        data = json.loads(tool_text)
    except ValueError:
        return tool_text
    for row in data.get("rows", []) if isinstance(data, dict) else []:
        for k in [k for k in row if k.endswith("(raw)")]:
            row.pop(k)
    return json.dumps(data)


@app.post("/api/chat")
async def chat(req: ChatRequest):
    messages = [{"role": "system", "content": state["system_prompt"]}] + req.messages[-10:]
    trace = []  # which tools were called, shown in the UI
    charts = []  # charts for the page to draw
    last_rows = []  # rows of the latest data result (feeds show_chart)
    nudged = False
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            msg = _call_llm(messages)
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = msg.get("content") or ""
                # Safety net: the model described a chart in text instead of calling show_chart -> ask once more
                if not charts and not nudged and re.search(r'"?(chart_type|stack_by|color_by)"?\s*:', answer):
                    nudged = True
                    messages.append({"role": "assistant", "content": answer})
                    messages.append({"role": "user", "content": "You wrote chart settings as text, so no chart "
                                     "was drawn. Call the show_chart tool instead (use color_by for one colour "
                                     "per value of a second field), then answer briefly without any JSON."})
                    continue
                if any(t["tool"] == "run_dax" and t["ok"] for t in trace):  # never let this be skipped
                    defaults = list(state["ui"].get("defaults", {}))
                    extra = f" (no {' / '.join(defaults)} defaults)" if defaults else ""
                    answer = (f"> ⚠️ Custom query on raw data rows{extra}, not a validated report measure.\n\n"
                              + answer)
                return {"answer": answer, "trace": trace, "charts": charts}
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}") or {}
                    if name == "show_chart":
                        charts.append(_build_chart(args, last_rows))
                        text, ok = "Chart shown to the user.", True
                    else:
                        result = await state["session"].call_tool(name, args)
                        text = result.content[0].text if result.content else ""
                        ok = not result.isError
                        if ok:
                            try:
                                last_rows = json.loads(text).get("rows") or last_rows
                            except (ValueError, AttributeError):
                                pass
                except Exception as e:  # bad arguments etc. -> let the LLM correct itself
                    args, text, ok = call["function"].get("arguments"), f"Error: {e}", False
                trace.append({"tool": name, "args": args, "ok": ok})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": _slim(text)})
        return {"answer": "Sorry, that took too many steps. Please rephrase the question.", "trace": trace,
                "charts": charts}
    except Exception as e:
        return {"answer": f"⚠️ {e}", "trace": trace, "charts": charts}


@app.get("/api/status")
async def status():
    return {
        "mcp_tools": [t["function"]["name"] for t in state.get("tools", [])],
        "llm_ready": bool(os.getenv("GROQ_API_KEY")),
        "model": os.getenv("GROQ_MODEL"),
    }


@app.get("/api/model")
async def model_info():
    """Title, greeting, suggestions and defaults for the page (from the MCP server's model://ui)."""
    return state.get("ui", {})


@app.get("/api/embed")
def embed():
    # Used when REPORT_EMBED_URL is blank: the page embeds the report with a token, no visitor sign-in
    import pbi_client
    try:
        return pbi_client.get_embed_config()
    except Exception as e:
        return {"error": str(e)}


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as f:
        html = f.read()
    return html.replace("{{REPORT_EMBED_URL}}", escape(os.getenv("REPORT_EMBED_URL", "")))


if __name__ == "__main__":
    import pbi_client
    if pbi_client.auth_mode() != "browser":
        # Cloud run (e.g. GitHub Codespaces): sign in now so the device code shows at startup
        pbi_client.sign_in()
        print("Power BI sign-in OK. Open the forwarded port 8000 to use the demo.", flush=True)
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
