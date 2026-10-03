# Trading control feedback update — 2026-10-03

The running service's HTML was updated atomically through Zeabur's service terminal. No Python process restart or trading endpoint call was made. The served HTML matches the local source after line-ending normalization.

Changes: inline armed/automatic states, explicit success/error feedback, busy/duplicate-click guard, enabled-state button labels, disabled redundant activation, trimmed confirmation text. Strategy and exchange code are unchanged.

Validation: JavaScript syntax and isolated UI behavior tests passed (off/on/automatic states, success, failure recovery and duplicate-click suppression). Live browser shows armed and automatic active; scan count continued increasing across page reload.

Keep this change on the fix branch until a planned maintenance window. Pushing main would redeploy the backend, which starts disarmed and would interrupt management of an active user-started position. The current in-container HTML hotfix is temporary and must be incorporated into the next built image; container recreation from the old image can revert the display fix. Do not restart a live trading service merely to change its UI.
