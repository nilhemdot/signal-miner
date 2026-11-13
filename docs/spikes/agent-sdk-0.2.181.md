# Spike: claude-agent-sdk 0.2.181 lockdown options

Checked 2026-11-13 by inspecting the installed package. No model call was made, so nothing here was observed at runtime.

## Results

- **Built-in tools:** `ClaudeAgentOptions.tools` exists and becomes the `--tools` flag on the CLI.
  - `tools=[]` sends `--tools ""`, which disables every built-in tool.
  - This is a stronger lock than `disallowed_tools`. Section 13 of the spec had asked whether `allowed_tools` restricts built-in tools; this answers it.
- **Tool lists:** `allowed_tools`, `disallowed_tools` and `can_use_tool` all exist.
- **Limits:** `max_turns` and `max_budget_usd` both exist. `max_budget_usd` becomes `--max-budget-usd`, a native spend cap for each `query()` call.
- **Cost:** `ResultMessage.total_cost_usd` holds the cost of a run.
- **In-process tools:** `create_sdk_mcp_server(name, version, tools)` plus the `@tool` decorator run MCP tools inside the Python process that calls `query()`.
- **Environment:** the CLI subprocess inherits the parent's full `os.environ`, minus `CLAUDECODE`, and adds `options.env` on top.

## Decision for slice 5

The env finding makes the separate `enrich` process in section 6 of the spec necessary.

- If `query()` ran in the main pipeline process, the CLI would inherit the DB URL, the PAT and every provider key.
- The `enrich` process starts with the stripped env that AC 21 checks for.
- Its in-process `web_search` and `web_fetch` tools call the parent's router over `ROUTER_URL` with `ROUTER_TOKEN`.

Lockdown, 4 layers:

1. `tools=[]`.
2. `allowed_tools` set to the 2 MCP tool names.
3. `disallowed_tools` set to the 7 built-in tool names.
4. A `can_use_tool` that denies anything else.

Two caps apply as well: `max_turns=12` and `max_budget_usd` per company.
