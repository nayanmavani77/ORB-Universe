# `reversal` — fade the opening-range breakout

A close **above** the range high **sells**. The bet is that the breakout fails
and price comes back into the range.

```yaml
sessions:
  london:
    engine: reversal
    range_start: "03:00"
    range_end:   "03:15"
    stop_time:   "09:30"
    engine_options:
      sl_range_mult: 0.75
      direction: reverse
      max_trades_per_session: 3
```

Everything except the stop and the direction is inherited from the breakout
engine and runs unmodified — the range build, the breakout test, the arming and
re-entry rules, the news filter, the stop time, the fill model. The class
overrides exactly two methods: `_stop_price` and `_open_trade`.

## Options

| option | default | meaning |
|---|---|---|
| `sl_range_mult` | `0.5` | stop distance as a multiple of the opening range |
| `direction` | `reverse` | `reverse` fades the breakout, `forward` follows it |
| `sl_anchor` | `range` | how a reversed stop distance is measured — `range` or `mirror` |
| `max_trades_per_session` | `0` | 1 = R, 2 = RR, 3 = RRR, 0 = unlimited |
| `order_tag` | `REV` | prefix in the MT5 order comment |

Old spellings still load: `max_trades`, `mult`, `anchor`, `tag`.

### `sl_range_mult` — the axis the original matrix never searched

| value | stop distance | |
|---|---|---|
| 0.25 | ¼ range | |
| **0.5** | ½ range | **identical to the breakout engine's `mid_range`** |
| 0.75 | ¾ range | |
| **1.0** | full range | **identical to its `full_range`** |
| 1.5 / 2.0 | wider | the breakout engine cannot express these |

0.5 and 1.0 reproduce the two fixed modes **trade for trade, to the cent** —
asserted in `tests/test_reversal_engine.py`. Every result produced before this
engine existed therefore stays comparable.

### `direction` — always run the control arm

`forward` runs the identical settings in the ordinary breakout direction. Run it
alongside every reversal: if forward is profitable too, the stop distance is
doing the work, not the fade.

### `sl_anchor` — only consulted when reversing

A reversed trade cannot reuse the original stop **level**: that level sits on the
wrong side of the entry, so the broker fills it instantly and every trade prints
breakeven. The reversed trade keeps the stop **distance** and puts it on the far
side of the entry.

- **`range`** (default) — `risk = sl_range_mult × range height`, measured from
  the fill. Independent of how far the breakout bar overshot.
- **`mirror`** — the exact distance the original trade would have taken,
  overshoot included, mirrored. Reproduces the earlier `tools/reversal_test.py`
  study, so old numbers can be re-derived.

## How the stop is placed

`range_size = range_high − range_low`.

**Forward** — a level measured inward from the side that broke:

```
BUY   stop = range_high − mult × range_size
SELL  stop = range_low  + mult × range_size
```

**Reversed** — the distance, on the far side of the fill:

```
reversed BUY   stop = entry − risk
reversed SELL  stop = entry + risk
```

Take profit is unchanged: `risk_reward × risk`, anchored on the actual fill.

## Files

| file | what it is |
|---|---|
| `strategy.py` | `ReversalStrategy` — overrides `_stop_price` and `_open_trade`, nothing else |
| `settings.py` | `ReversalSettings` — the options above |
| `grid.py` | the sweep axes, including `sl_range_mult` |
| `__init__.py` | registers the engine under the name `reversal` |

## A note on how this used to work

The engine was reached by rebinding `orb.engine.RangeBreakoutStrategy` — a
process-wide monkey-patch. That made it impossible to run this engine and the
breakout engine at the same time, and it gave the reversal no live path at all,
because `LiveTrader` never applied the patch. Selection now happens per session
through `orb/registry.py`, so both work.
