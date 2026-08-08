# [PROF-XXXX] - TICKET_NAME

## Description
<!-- What does this PR change or add? What tools does this change affect? -->

## Related APIs/PRs
<!-- If this is a new tool, what endpoint does it use? What PR exposed that endpoint? -->

## Type of change
- [ ] New tool/resource/prompt
- [ ] Bug fix
- [ ] Tool schema change
- [ ] Transport/connection change
- [ ] Documentation update
- [ ] Other

---

## MCP Testing Checklist
[SETUP LOOM](https://www.loom.com/share/c8f45c270f974644b57e493a505c074a)

### 1. Local server startup
- [ ] Run (`uv sync`)
- [ ] Server starts without errors (`uv run python -m goprofiles_mcp`)
- [ ] No unhandled exceptions in logs on startup

### 2. ngrok setup 
<!-- Skip if you're only using MCP Inspector / Claude Desktop against localhost. -->
- [ ] Check if installed: `ngrok version` — if missing, install with `brew install ngrok` ([download](https://ngrok.com/download) for other OS)
- [ ] Authtoken configured once: `ngrok config add-authtoken YOUR_AUTHTOKEN` (from [ngrok dashboard](https://dashboard.ngrok.com/get-started/your-authtoken))
- [ ] With the MCP server running on port `8000`, start a tunnel in a separate terminal: `ngrok http 8000`
- [ ] Copy the HTTPS forwarding URL (e.g. `https://abc123.ngrok-free.app`) and use `…/mcp` as the connector URL (we will use this later)

- [ ] Keep both the MCP server and ngrok running for the rest of testing

### 3. ChatGPT setup
- [ ] Settings on ChatGPT -> Plugins -> enable Developer mode
- [ ] Plugins -> plus button next to the search bar
- [ ] Fill in proper info (Connection = your ngrok url appended with /mcp, Authentication = OAuth)
- [ ] OAuth advanced settings filled (Client id = chatgpt-testing, everything else should auto fill)
- [ ] Go through OAuth process
- [ ] In the specific connector settings, confirm all expected tools show up


### 4. Tool-by-tool verification
<!-- List each tool touched by this PR and confirm it works -->
- [ ] `tool_name_1` — tested with valid input, returns expected result
- [ ] `tool_name_1` — tested with invalid/missing input, fails gracefully with a clear error
- [ ] `tool_name_2` — ...
- [ ] Ensure existing tools still work


## Screenshots / logs
<!-- Paste Inspector output, terminal logs, or screenshots showing the test results -->

## Additional notes
<!-- Anything reviewers should know before testing this themselves -->