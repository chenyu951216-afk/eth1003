# S300 Bitget dependency map

Reference: `chenyu951216-afk/s300` main at `09656136af3ff06611f82be9a1693e3bc2687df7`.

## Core adapter retained

`app/bitget.py` is copied into `eth1003/exchange/bitget.py`. The retained API methods are `request`, `sync_time`, `account_type`, `settings`, `balances`, `balance_usdt`, `instrument`, `tier`, `ticker`, `positions`, `pending`, `plans`, `position_history`, `recent_orders`, `order_by_id`, `account_snapshot`, `prepare`, `entry_payload`, `place`, `modify_entry`, `detail`, `cancel`, `reduce`, `add_plan`, `cancel_plan`, and `diagnostics`; strict response helpers `_rows`, `quantity`, and `_crossed` stay with it. `preview` remains for source traceability but is **not called** because it applies S300's amount defaults and selects the exchange maximum leverage. New `TradeIntent` validation and sizing are upstream. Adapter changes are limited to optional market entry without limit price or force and an explicit 150x read-back gate before `place`; both reuse the same S300 endpoint and request/signing path.

## Minimal imported dependencies

`app/signals.py`: `decimal`, `fmt`, `floor_step`, `ceil_step`, and `validate_trade` are needed by the copied adapter. No Telegram parser, provider rules, or signal interpretation is imported. `app/store.py`: exact compact `dumps` function; a narrow `get`/`redact`/`event` bridge supplies environment credentials and account type. Secrets never enter the local ledger. The original encrypted S300 Store and its Telegram tables are not copied.

## Lifecycle behavior mapped from app/engine.py

`oid` creates stable unique client OIDs; `filled` refuses missing fill quantities; `plan_price`/`covers` verify exchange-native TP/SL coverage. `submit` first persists an intent, then does preflight, account setup and placement; network or 5xx uncertainty never causes another financial POST. `reconcile` reads order detail, position, pending and plan state, distinguishes accepted/pending/partial/filled/canceled, and fails closed on inconsistent ownership. Management operations persist their OIDs before cancel/modify/reduce/plan POST and reconcile afterward. Full close and partial close use S300 `reduce`, not a fresh opposite-side entry.

## Deliberate differences from S300

`TARGET_LEVERAGE=150` is a hard condition checked against instrument and matching position tier, then verified by S300 `prepare` after set-leverage. The strategy supplies the exact requested notional; execution only floors quantity to exchange step and validates minimum/maximum. The ETH strategy uses market entry/exit after a completed 1h Binance ETHUSDT perpetual bar, matching the frozen historical signal model. TP/SL are optional because the frozen EMA strategy has no fixed TP or SL; setting either changes the historical strategy and is not silently introduced. No Telegram, channel, AI, S300 strategy, or S300 notional defaults are used.

## Verification boundary

The S300 code is a user-verified reference. This repository tests its adapted behavior with mock exchange replies and public market data. Real account mode, Demo/Live acceptance, actual 150x availability and fills require the user's Bitget account and cannot be claimed from mocks. Financial POSTs are off by default.
