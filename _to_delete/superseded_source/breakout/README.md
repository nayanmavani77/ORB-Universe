# `breakout` — the opening-range breakout

The original strategy, a 1:1 port of `RangeBreakoutEA.mq5` v1.70.

```yaml
sessions:
  new_york:
    engine: breakout
    range_start: "09:30"
    range_end:   "10:00"
    stop_time:   "16:55"
```

## The rules

1. **Range** = high/low of the signal-timeframe bars between `range_start` and
   `range_end`, built once on the first tick at or after the window ends.
   `mid = (high + low) / 2`.
2. **Entry** on a *closed* bar whose close is beyond the range high (BUY) or the
   range low (SELL). The bar must have closed after the range window ended.
3. **Stop** at the range midpoint (`sl_mode: mid_range`) or the opposite side of
   the range (`sl_mode: full_range`).
4. **Target** = `risk_reward × |fill price − stop|`, applied after the fill.
5. **Arming**: the range arms the first breakout. After a fill the strategy
   disarms; with `require_range_reentry` it re-arms only when a bar closes back
   inside the range, otherwise on the first closed bar while flat.
6. One position at a time, optional `max_trades_per_session`, news filter.
7. The trading window runs from `range_end` to `stop_time`.

## Options

**None.** Every setting this engine uses is a standard session field —
`signal_timeframe`, `sl_mode`, `risk_reward`, `lots`, `require_range_reentry`,
`max_trades_per_session`, `close_at_stop_time`, and the news filter. They live in
`StrategyConfig` because this was the only engine when they were designed, and
moving them would break a config schema that already works.

`BreakoutSettings` is therefore an empty settings class. It earns its place by
rejecting options that belong to another engine:

```yaml
engine: breakout
engine_options:
  sl_range_mult: 0.75      # error at load — that is a reversal option
```

Without it, that line would be silently ignored and the backtest would run with
defaults.

## Files

| file | what it is |
|---|---|
| `strategy.py` | `RangeBreakoutStrategy` — was `orb/strategy.py`, logic untouched |
| `settings.py` | `BreakoutSettings` — empty, see above |
| `grid.py` | the sweep axes `tools/run_matrix.py` uses |
| `__init__.py` | registers the engine under the name `breakout` |
