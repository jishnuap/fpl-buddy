# Before you set `DRY_RUN=false`

The two write payloads in `fpl/client.py` were assembled from community sources,
not an official spec. They are probably right. "Probably" is not good enough for
code that can spend a season's points, so do this once.

You need one real transfer you were going to make anyway, and DevTools.

## 1. Capture the real transfer request

1. Open fantasy.premierleague.com → Transfers, with DevTools → Network open,
   filter to `Fetch/XHR`.
2. Make the transfer in the UI and confirm it.
3. Find the `POST` to `/api/transfers/`. Right-click → Copy → Copy as cURL.

## 2. Capture the real picks request

1. Go to My Team, change the captain or reorder the bench, and save.
2. Find the `POST` to `/api/my-team/{entry}/`. Copy as cURL.

## 3. Get what this code would have sent

With `DRY_RUN=true`, the client logs the exact payload instead of sending it:

```bash
.venv/bin/fpl-buddy propose
.venv/bin/fpl-buddy approve --yes -v      # DRY RUN -- prints both payloads
```

Look for the `DRY RUN -- not submitting` lines.

## 4. Diff them

Check, in order:

- **Field names and nesting.** `transfers` is a list of
  `{element_in, element_out, purchase_price, selling_price}`; the envelope carries
  `confirmed`, `entry`, `event`, `chip`, `freehit`, `wildcard`.
- **Prices.** `selling_price` must equal what your own `my-team` pick reported,
  not the player's `now_cost`. They differ whenever a player has risen since you
  bought them (you get half the rise). If these are wrong FPL rejects the request.
- **Picks array length and order.** All 15 slots, `position` 1–15, 1–11 the XI,
  12–15 the bench in auto-sub order, and **12 must be the reserve keeper**.
- **Headers.** `Content-Type: application/json`,
  `X-Requested-With: XMLHttpRequest`, `Origin: https://fantasy.premierleague.com`,
  a matching `Referer`, and the cookie header.
- **Chip flags.** `chip`, `freehit` and `wildcard` on the transfers call;
  `chip` on the picks call for `bboost` / `3xc`.

Anything that differs: fix `submit_transfers` / `submit_picks`, and add a test in
`tests/test_client.py` that pins the corrected shape.

## 5. Then go live carefully

```bash
DRY_RUN=false .venv/bin/fpl-buddy propose
DRY_RUN=false .venv/bin/fpl-buddy approve          # prompts before submitting
```

Do the first live run yourself, in front of the terminal, with a gameweek's worth
of slack — not by letting the deadline job do it unattended. Verify in the app
that what landed is what you expected, and only then leave `AUTO_COMMIT_ENABLED`
to do its thing.

A useful intermediate setting: `EXECUTE_ON_APPROVAL=false`. Approval then just
records intent and the commit job submits at T-45m against fresh data, which is
the safer ordering once you trust the payloads.
