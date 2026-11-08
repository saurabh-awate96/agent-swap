# JSON output reference

`aswap list --json`, `aswap status --json` and `aswap switch --json` print a single JSON object on stdout; human-readable notices go to stderr. The commands are listed in [commands.md](commands.md). Codex's `--json` output is different: see [its section below](#codex-json).

Example:

```bash
aswap list --json                   # all accounts with usage and quota
aswap status --json                 # the account in use
aswap switch --strategy best --json # switch, then report the result
aswap switch 2 --json
```

```json
{
  "schemaVersion": 1,
  "activeAccountNumber": 2,
  "accounts": [
    { "number": 2, "email": "you@example.com", "active": true, "usageStatus": "ok",
      "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                 "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } }
  ]
}
```

## Contract

- Every payload carries a `schemaVersion` (currently `1`).
- On a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code.
- `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

## Usage windows

Each account row has a `usage` object with `fiveHour` and `sevenDay` windows (`pct`, `resetsAt`).

- A window's `pct` is `null` when the usage API reported `utilization: null` for it.
- A window the account does not have (an Ultra account's `fiveHour`) is omitted.
- Usage is served from a per-account cache: when the usage API is briefly unreachable, the last-known numbers are shown instead of nothing (the human view marks them with their age, e.g. `· 2m ago`).

## Optional row fields

These apply to `list` rows and the managed active row from `status --json`. All are additive.

| Field | Present when | Meaning |
|---|---|---|
| `usageFetchedAt`, `usageAgeSeconds` | the row has decision-trusted usage | How old the measurement is. |
| `lastGoodUsage`, `lastGoodFetchedAt`, `lastGoodAgeSeconds` | `usage` is null but a last-known measurement exists: data too old to drive a decision (`usageStatus` stays `unavailable`), or a row in a non-`ok` state such as `token_expired` | Preserves the human display without making the account actionable. |
| `usageError` | `usage` is null and nothing else explains it (`usageStatus` is `unavailable`) | The last fetch failure by kind (e.g. `http-429`, `timeout`). |
| `usageRetryAt` | the cache is backing off from that failure | The time of the next attempt. |
| `disabled` | the account is held out of rotation with `aswap disable` | `true`; absent otherwise. |
| `threshold` | a per-account threshold is set with `aswap threshold` | e.g. `85.0`; absent otherwise, so a fleet that never sets a policy emits rows identical to before. |
| `alias` | an alias is set with `aswap alias` | e.g. `"dev"`; accounts without one omit the key. |
| `loginExpiresAt` | the stored login records when its refresh token expires | ISO-8601 UTC: the moment the slot will need a fresh `/login` and `aswap add --slot N`, so a script can warn a few days ahead instead of discovering `relogin_required`. Absent when Claude Code recorded no such date for that login. |
| `plan` | the slot's subscription plan has been read from the profile endpoint at least once | `label` as the human views print it (`Ultra`, `Max 5x`, `Pro`, `Team`, `Free`), the raw `tier`, then `organizationType`, `billingType`, `subscriptionStatus` and `planDisplayName` when the profile carried them, `expired` (`true` once the subscription is `canceled` and the account is back on the free tier; the human views show that as a highlighted `Free`) and `fetchedAt`, the last successful profile read. Plans refresh on their own six-hour cadence, not with usage. |

## Pace fields on weekly windows

Weekly windows (`sevenDay` and per-model `scoped` entries, never `fiveHour`) additively carry pace fields once the week is about a day old.

| Field | Meaning |
|---|---|
| `expectedPct` | Where usage would sit if spread evenly across the week. |
| `aheadOfPace` | `true` when meaningfully above that: the same signal the human views show as an `(ahead)`/`(ahead of pace)` marker. |
| `projectedExhaustionAt` | The current rate extrapolated into an ETA to 100%. |
| `willLastToReset` | Whether the quota will last to the reset at the current rate. |

The two projections stay `--json`-only, since a linear projection is too rough to present as fact in the UI.

## `aswap auto --json`

`aswap auto --json` emits an event stream instead: one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}`) with kinds like `poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `error`. The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.

## Codex JSON

`aswap codex list --json` and `aswap codex status --json` are separate from the Claude payloads above, and they do **not** carry a `schemaVersion` yet:

- `aswap codex list --json` prints a JSON array, one object per stored account: `slot`, `alias`, `email`, `plan`, `authMode`, `active`, `disabled`, `threshold`, `problem`, `health` and `usage`. `usage` holds the Codex windows as Codex recorded them (`primary` and `secondary`, each with `used_percent`, `window_minutes` and `resets_at`, a Unix time).
- `aswap codex status --json` prints one object: `state` (`managed`, `unmanaged`, `absent` or `unreadable`), and for a managed or unmanaged login `slot`, `email`, `plan`, `authMode`, `lastRefresh`, `accessExpiresInSeconds` and `usage`.

Scripts should read the fields they need by name and ignore the rest. Until these payloads carry a `schemaVersion`, their shape may change between releases; the change will be listed in the [changelog](../CHANGELOG.md).
