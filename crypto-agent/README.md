# crypto-agent

An autonomous crypto trading agent for your Robinhood account. Claude analyzes market data and proposes trades. A deterministic risk engine checks every proposal, and only the orders it approves are placed through the **Robinhood Trading MCP** (`https://agent.robinhood.com/mcp/trading`).

> **Read this first.** This is not financial advice, and nothing here promises profit. No AI is "the best trader in the world." Crypto is volatile, and **you can lose up to the full allowance you give the agent**. You alone are responsible for every trade it places, for your taxes, and for following Robinhood's terms. Start in paper mode, and only fund an allowance you can afford to lose.

## How it works

```
every 15 min:  STOP file? ─▶ quotes + your positions ─▶ code-enforced stop-loss / take-profit
               ─▶ indicators (RSI, MACD, EMA, ATR, volatility) + optional news search
               ─▶ Claude proposes buy/sell/hold as JSON  ─▶  RISK ENGINE (clips or rejects)
               ─▶ approved orders only ─▶ Robinhood MCP ─▶ fill confirmed ─▶ ledger + audit log
```

- **Claude never touches your account.** It only writes proposals. The agent's own code is the MCP client, and it can call just the five tools you map in `config.yaml`: quote, buying power, holdings, place order, get order. Any other tool on the server, such as transfers or withdrawals, can't be reached from this code.
- **Your allowance is the ceiling.** The agent trades only what Robinhood reports as its buying power. All limits are percentages of agent equity (allowance cash plus the positions the agent opened), so they scale with whatever you fund.
- **It never sells crypto it didn't buy.** Coins you already held stay out of its reach.

## Risk limits (`config.yaml` → `limits`)

| Setting | Default | Effect |
|---|---|---|
| `max_order_pct` | 25 | Largest single order, as a % of equity |
| `max_daily_loss_pct` | 10 | A loss this big in a UTC day stops new buys until tomorrow |
| `max_drawdown_pct` | 30 | A loss this big since the start halts the agent until you run `--reset-halt` |
| `max_total_exposure_usd` | `null` | Optional extra dollar cap. `null` means the allowance is the only cap |
| `max_trades_per_day` | 6 | Claude-initiated trades. Stop-loss and take-profit exits are always allowed |
| `cooldown_minutes_per_symbol` | 60 | Prevents churn on a single coin |
| `min_confidence` | 0.6 | Proposals with lower confidence are rejected |
| stop-loss / take-profit | 5% / 10% | Claude can choose within 1–15% / 2–50%, and the code enforces them every cycle |
| `allowed_symbols` | BTC, ETH, SOL | Nothing else can be traded |

There's no leverage, no shorting and no margin.

## Setup (on your own computer)

Requires Python 3.11+.

```bash
cd crypto-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY; leave TRADING_MODE=paper for now
```

1. **Fund an allowance** for the agent in Robinhood.
2. **(Optional) Add the MCP server to Claude Code** so you can look things up interactively:
   `claude mcp add --transport http robinhood-trading https://agent.robinhood.com/mcp/trading`, then `/mcp` to log in.
   This repo's `.claude/settings.json` makes Claude Code **ask before every** Robinhood MCP call. Trades you place this way are **outside** the agent's limits and its audit log.
3. **Log the agent in:** `python -m agent.main --login`. A browser opens on Robinhood's login page. Before you enter anything, **check that the address is a `robinhood.com` page.** The agent refuses to open login pages on any other domain. Tokens are saved to `state/mcp_oauth.json` (owner-only permissions, git-ignored).
4. **Discover the tools:** `python -m agent.main --discover` saves the server's tool list to `state/mcp_tools.json`. It's read-only and calls no trading tools. **Send that file to Claude** to fill in the `mcp.tools` mapping in `config.yaml` (it holds no secrets). Until every `TBD` is filled in, the agent refuses to trade live.
5. **Paper trade for at least a few days:** `python -m agent.main`. Read `logs/audit.jsonl` to judge its decisions. Set `paper.starting_cash_usd` to your allowance so the numbers are comparable.
6. **Go live:** in `.env`, set `TRADING_MODE=live` and `I_ACCEPT_LIVE_TRADING_RISK=yes`, then run `python -m agent.main`.

If Robinhood's login rejects this custom client (some MCP servers only accept approved AI apps), tell Claude. The fallback is to run the same loop through the Claude Agent SDK, using Claude Code's existing Robinhood login, with the risk engine screening each order.

## Day-to-day commands

| Command | What it does |
|---|---|
| `python -m agent.main` | Run continuously (a cycle every `cycle_minutes`) |
| `python -m agent.main --once` | Run a single cycle |
| `touch STOP` | **Kill switch.** No orders are placed while this file exists. Delete it to resume |
| `python -m agent.main --status` | Show positions, P&L and halt state |
| `python -m agent.main --reset-halt` | Clear a halt after you've checked the Robinhood app |
| `python -m agent.main --rebaseline` | After changing the allowance, restart loss limits from current equity |
| `python -m agent.main --offline --once` | Simulated prices and a paper broker, for testing |

**If the agent halts with "order state unknown":** an order may or may not have gone through. Open the Robinhood app, check your orders, and only then run `--reset-halt`. The agent never retries an order on its own, so a network glitch can't double-buy.

## Records

- `logs/audit.jsonl` is an append-only record of every Claude proposal and its reasoning, every risk verdict, and every order and fill. Keep it for taxes and for any dispute.
- `state/` holds the ledger, OAuth tokens and the paper account. It's git-ignored, so never commit or share it.

## Tests

```bash
pytest
```

The tests cover:
- the risk limits at a $5,000 allowance
- a full trading cycle against an **in-process fake Robinhood MCP server**
- an order timeout halting the agent without a retry
- the phishing guard and token-file permissions
- malformed or hostile Claude output being treated as "hold"
