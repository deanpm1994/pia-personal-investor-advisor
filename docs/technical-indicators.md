# Deterministic EOD technical-indicator policy

Issue #96 implements provider-independent SMA-20, SMA-50, SMA-200, and
Wilder RSI-14 analysis from validated persisted daily EOD bars. These values
are analysis evidence only. They are not recommendations, predictions,
certainty statements, or guaranteed returns.

## Input and ordering

- One calculation accepts bars for exactly one immutable listing, provider
  symbol, MIC, quote currency, and mapping version. Mixed identities make the
  analysis unavailable.
- Input order is irrelevant. Bars are ordered by `market_date` after revision
  resolution.
- Exact duplicates with the same date and revision collapse idempotently.
  Contradictory duplicates with the same date and revision make the complete
  analysis unavailable; the calculator never chooses between them.
- When several revisions exist for a date, only the highest revision is used.
  SMA results are marked corrected while that date remains in their rolling
  window. Wilder RSI results remain marked corrected thereafter because its
  smoothed state retains the correction.

## Windows, gaps, and warm-up

- SMA uses the arithmetic mean of the latest 20, 50, or 200 observed closes,
  including the result date.
- RSI uses Wilder smoothing over 14 changes. Its first value therefore needs
  15 closes. The initial average gain and loss are arithmetic means; later
  averages use `(previous_average * 13 + current_change) / 14`.
- If both average gain and loss are zero, RSI is the neutral value 50. If only
  average loss is zero, RSI is 100; if only average gain is zero, RSI is 0.
- Warm-up points are emitted with `insufficient_history`, a null value, and
  their exact observed/required counts.
- Missing weekdays are never filled, copied forward, or treated as zero-volume
  bars. Calculations continue across observed sessions but every affected
  result is explicitly `incomplete` with an `INDICATOR_CALENDAR_GAP`
  diagnostic. This does not claim the gap was or was not an exchange holiday.

## Precision and evidence

- All calculations use `Decimal` with an internal precision of 50 digits.
  Published values are rounded once to 12 decimal places with round-half-even.
- Each result carries listing/provider identity, quote currency, the complete
  source-URL set, maximum provider-as-of and retrieval timestamps for its
  evidence window, freshness, completeness, and correction state.
- An incomplete source bar makes each result whose evidence includes it
  incomplete. Freshness follows ADR 0009 weekday aging from the newest bar:
  current is `fresh`, one weekday behind is `pending`, and two or more is
  `stale`. Pending and stale series also carry stable per-result diagnostics.
- Empty input, future-dated input, mixed identities, and contradictory
  duplicates return an explicit unavailable analysis with no indicator values.
