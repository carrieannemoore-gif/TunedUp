# crypto-agent

An autonomous crypto trading agent for your Robinhood account. Claude analyzes market data and proposes trades. A deterministic risk engine checks every proposal. Only approved orders are placed, through **your own `robinhood-crypto-mcp` server** ([repo](https://github.com/carrieannemoore-gif/carrieannemoore-gif-robinhood-crypto-mcp)), which talks to Robinhood's official Crypto Trading API.

> **Read this first.** This is not financial advice, and nothing here promises profit. No AI is "the best trader in the world." Crypto is volatile, and **you can lose the full allowance you give the agent**. You alone are responsible for every trade it places, for your taxes, and for following Robinhood's API terms. Start in paper mode, and only set an allowance you can afford to lose.

## How it works

```
every 15 min:  STOP file? ─▶ quotes + your positions ─▶ code-enforced stop-loss / take-profit
               ─▶ indicators (RSI, MACD, EMA, ATR, volatility) + optional news search
               ─▶ Claude proposes buy/sell/hold as JSON  ─▶  RISK ENGINE (clips or rejects)
               ─▶ approved orders only ─▶ robinhood-crypto-mcp (its own cap + allowlist) ─▶ Robinhood
               ─▶ fill confirmed ─▶ ledger + audit logs
```

- **Claude never touches your account.** It only writes proposals. The agent's own code starts your MCP server and calls six mapped tools: `get_best_bid_ask`, `get_account`, `get_holdings`, `get_trading_pairs`, `place_order` and `list_orders`. It **never calls `cancel_order`** or any other tool.
- **The allowance is the agent's whole world.** `limits.allowance_usd` works like a sub-account. The agent can spend the allowance, plus realized profit or loss, minus money in its open positions, and never more than your account really has. Your account's total buying power is **never** used as the budget. **Live trading is refused until you set it.**
- **There are two independent safety layers.** The agent's risk engine sizes and screens every order. Then your server enforces its own per-order dollar cap (`RH_MAX_ORDER_NOTIONAL_USD`) and the same coin allowlist (the agent sets `RH_ALLOWED_SYMBOLS` from its own list).
- **It never sells crypto it didn't buy.** Coins you already held stay out of its reach.
- **It never retries an order.** If a failure could mean the order went through, it looks the order up by its `client_order_id`. If the order can't be found, the agent **halts** until you check the Robinhood app.

## Risk limits (`config.yaml` → `limits`)

| Setting | Default | Effect |
|---|---|---|
| `allowance_usd` | `null` (**required for live**) | The agent's budget: the most it can ever have invested |
| `max_order_pct` | 25 | Largest single order, as a % of agent equity (cash plus agent positions) |
| `max_daily_loss_pct` | 10 | A loss this big in a UTC day stops new buys until tomorrow |
| `max_drawdown_pct` | 30 | A loss this big since the start halts the agent until you run `--reset-halt` |
| `max_total_exposure_usd` | `null` | Optional extra cap on the market value of agent positions |
| `max_trades_per_day` | 6 | Claude-initiated trades. Stop-loss and take-profit exits are always allowed |
| `cooldown_minutes_per_symbol` | 60 | Prevents churn on a single coin |
| `min_confidence` | 0.6 | Proposals with lower confidence are rejected |
| stop-loss / take-profit | 5% / 10% | Claude can choose within 1–15% / 2–50%, and the code enforces them every cycle |
| `allowed_symbols` | BTC, ETH, SOL | Nothing else can be traded, by the agent or the server |

There's no leverage, no shorting and no margin.

## Setup (on your own computer)

Requires Python 3.11+ and Node 20+.

1. **Build your MCP server:** in your `robinhood-crypto-mcp` checkout, run `npm install && npm run build`. Create a key pair with `npm run keygen -- ~/.robinhood/private.key`, register the printed public key in Robinhood, and copy the `rh-api-…` key.
2. **Set up the agent:**
   ```bash
   cd crypto-agent
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```
   In `.env`, set `ANTHROPIC_API_KEY`, `ROBINHOOD_API_KEY`, `ROBINHOOD_PRIVATE_KEY_BASE64` (the contents of the key file) and `RH_MAX_ORDER_NOTIONAL_USD`. Keep `TRADING_MODE=paper` for now. The Robinhood keys are passed **only** to the server process the agent starts. Your Anthropic key and other environment variables are not passed to it.
3. **Point the agent at the server.** In `config.yaml`, set `mcp.args` to the absolute path of `dist/index.js`.
4. **Choose the allowance.** In `config.yaml`, set `limits.allowance_usd`, for example `1000`. In `.env`, set `RH_MAX_ORDER_NOTIONAL_USD` to at least `allowance × max_order_pct / 100` (for example $250 on a $1,000 allowance). Otherwise the server will refuse the agent's larger orders.
5. **Read-only check against your real account:** `python -m agent.main --check`. It starts your server **with trading disabled** and prints quotes, buying power, holdings and order-size increments, exactly as the agent will read them. Your server's own README says it **hasn't been run against the live API yet**, so this is the step that proves it. Compare the numbers with the Robinhood app.
6. **Paper trade for at least a few days:** `python -m agent.main`. Read `logs/audit.jsonl` to judge its decisions. Set `paper.starting_cash_usd` to your allowance.
7. **Go live:** in `.env`, set `TRADING_MODE=live` and `I_ACCEPT_LIVE_TRADING_RISK=yes`, then run `python -m agent.main`. At startup the agent checks every tool call it will make against your server's real schemas. That includes `confirm: true`, without which the server would only preview orders. It refuses to start if anything doesn't match.

**A decision you've made, recorded here.** Your server's README advises keeping a person-approves-each-order prompt on `place_order`. Running this agent autonomously replaces that prompt with the agent's risk engine plus the server's cap and allowlist. That was your choice. If you'd rather approve trades yourself, use the server from Claude Code instead. This repo's `.claude/settings.json` makes Claude Code ask before every `robinhood-crypto` and `robinhood-trading` tool call.

**How the agent reads your server's errors.** From the `claude/order-outcome-errors` change onward, every `place_order` error from your server starts with one of three prefixes:
- `Order NOT sent:` and `Order rejected by Robinhood, not executed:` are clean refusals, and the agent moves on.
- `Order outcome UNKNOWN` always makes the agent look the order up by `client_order_id`. If it can't find the order, it halts.

Your server also checks the coin allowlist before any network call. With an older build, a failed price estimate looks the same as a failed send, so the agent halts as a precaution. If that happens, check the app, then run `--reset-halt`.

**To use Robinhood's own MCP server instead:** set `mcp.transport: http` and `mcp.url`, run `--login`, then `--discover`, and update `mcp.tools` to match its tools.

## Day-to-day commands

| Command | What it does |
|---|---|
| `python -m agent.main` | Run continuously (a cycle every `cycle_minutes`) |
| `python -m agent.main --once` | Run a single cycle |
| `touch STOP` | **Kill switch.** No orders are placed while this file exists. Delete it to resume |
| `python -m agent.main --check` | Read-only test through your server (trading disabled) |
| `python -m agent.main --discover` | Save the server's tool list to `state/mcp_tools.json` (calls no tools) |
| `python -m agent.main --status` | Show positions, P&L and halt state |
| `python -m agent.main --reset-halt` | Clear a halt after you've checked the Robinhood app |
| `python -m agent.main --rebaseline` | After changing the allowance, restart loss limits from current equity |
| `python -m agent.main --offline --once` | Simulated prices and a paper broker, for testing |

## Records

- `logs/audit.jsonl` records every Claude proposal and its reasoning, every risk verdict, and every order and fill, from the agent's side.
- `logs/mcp_server_audit.jsonl` is your server's own record of every order it submitted and the result. `logs/mcp_server_stderr.log` holds its diagnostics.
- Keep the logs for taxes and for any dispute. `state/` (the ledger and the paper account) is git-ignored, so never commit or share it.

## Tests

```bash
pytest                                                       # 62 tests, no network needed
RH_MCP_SERVER_DIST=/path/to/robinhood-crypto-mcp/dist/index.js pytest   # + 5 tests against your real server
```

The tests cover:
- the allowance (never your whole account), the risk limits and the kill switch
- the shipped tool mapping, checked against your server's real tool schemas
- order outcomes: confirmed fills, refusals by the cap or allowlist, a sent order whose reply was lost (found again, never re-sent), and an ambiguous failure (halt)
- the stdio launcher, so keys reach the server and nothing else does
- the integration tests, which run **your actual compiled server** against a local Robinhood API mock that verifies every request signature
