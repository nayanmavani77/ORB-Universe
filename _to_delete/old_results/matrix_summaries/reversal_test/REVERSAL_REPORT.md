# Reversal test — the 10 worst configurations at R:R 1.0 / 1.5 / 2.0 / 2.5

Period 2026-01-01 to 2026-08-13, gross P&L. Every variant was **re-simulated on
the bar data** — see the note at the end on why the trade CSVs cannot answer this.

## Variants

| | rule |
|---|---|
| ORIGINAL | the configuration exactly as the matrix ran it |
| R | reverse trade #1, then stop trading that session |
| RR | reverse trades #1 and #2, then stop |
| RRR | reverse trades #1, #2 and #3, then stop |

The reversed trade keeps the **same risk distance** the original would have taken,
placed on the other side of the entry. Target follows from R:R as usual.

## Totals across all 10

| variant | configs | trades | net P&L | profitable | median max DD |
|---|---:|---:|---:|---:|---:|
| **ORIGINAL** | 10 | 3,614 | -654,861 | 0/10 | 76.5% |
| **R** | 10 | 1,220 | 93,581 | 7/10 | 15.8% |
| **RR** | 10 | 2,049 | 235,147 | 9/10 | 12.4% |
| **RRR** | 10 | 2,468 | 262,750 | 8/10 | 11.7% |

## Per configuration

### M15_LONDON_ORB15_SKIP_NEWS_RR2

| variant | trades | win rate | net P&L | profit factor | max DD |
|---|---:|---:|---:|---:|---:|
| ORIGINAL | 273 | 23.8% | -76,205 | 0.60 | 80.1% |
| R | 107 | 43.9% | 11,400 | 1.16 | 16.1% |
| RR | 171 | 46.2% | 34,605 | 1.36 | 12.2% |
| RRR | 196 | 47.5% | 43,300 | 1.41 | 9.9% |

### M5_LONDON_ORB30_SKIP_NEWS_RR2

| variant | trades | win rate | net P&L | profit factor | max DD |
|---|---:|---:|---:|---:|---:|
| ORIGINAL | 328 | 28.1% | -68,195 | 0.69 | 78.5% |
| R | 106 | 38.7% | 6,865 | 1.10 | 13.3% |
| RR | 184 | 38.0% | 5,555 | 1.05 | 17.5% |
| RRR | 222 | 38.7% | 13,295 | 1.10 | 16.8% |

### M5_LONDON_ORB15_SKIP_NEWS_RR2

| variant | trades | win rate | net P&L | profit factor | max DD |
|---|---:|---:|---:|---:|---:|
| ORIGINAL | 406 | 28.3% | -66,475 | 0.71 | 75.2% |
| R | 107 | 43.9% | 40,145 | 1.89 | 4.6% |
| RR | 189 | 42.3% | 53,155 | 1.62 | 5.7% |
| RRR | 240 | 40.4% | 50,210 | 1.43 | 8.7% |

### M15_LONDON_ORB15_SKIP_NEWS_RR1p5

| variant | trades | win rate | net P&L | profit factor | max DD |
|---|---:|---:|---:|---:|---:|
| ORIGINAL | 276 | 30.1% | -66,282 | 0.63 | 74.7% |
| R | 107 | 53.3% | 16,928 | 1.27 | 15.6% |
| RR | 182 | 52.8% | 37,858 | 1.40 | 14.0% |
| RRR | 218 | 53.7% | 49,622 | 1.47 | 11.0% |

### M15_LONDON_ORB15_SKIP_NEWS_RR2p5

| variant | trades | win rate | net P&L | profit factor | max DD |
|---|---:|---:|---:|---:|---:|
| ORIGINAL | 273 | 23.1% | -65,368 | 0.66 | 71.9% |
| R | 107 | 39.2% | 13,882 | 1.19 | 17.1% |
| RR | 166 | 43.4% | 38,047 | 1.39 | 12.4% |
| RRR | 179 | 43.0% | 38,137 | 1.36 | 10.5% |

### M1_LONDON_ORB30_SKIP_NEWS_RR2

| variant | trades | win rate | net P&L | profit factor | max DD |
|---|---:|---:|---:|---:|---:|
| ORIGINAL | 428 | 29.2% | -63,245 | 0.74 | 72.7% |
| R | 106 | 34.0% | -1,785 | 0.97 | 13.2% |
| RR | 194 | 35.6% | 7,555 | 1.07 | 12.4% |
| RRR | 259 | 37.1% | 19,410 | 1.14 | 13.0% |

### M1_LONDON_ORB60_INCLUDE_NEWS_RR1p5

| variant | trades | win rate | net P&L | profit factor | max DD |
|---|---:|---:|---:|---:|---:|
| ORIGINAL | 450 | 36.4% | -63,238 | 0.79 | 81.6% |
| R | 158 | 45.6% | 3,167 | 1.03 | 18.1% |
| RR | 279 | 45.5% | 25,549 | 1.15 | 10.0% |
| RRR | 351 | 44.2% | 18,891 | 1.09 | 12.3% |

### M5_LONDON_ORB60_INCLUDE_NEWS_RR2

| variant | trades | win rate | net P&L | profit factor | max DD |
|---|---:|---:|---:|---:|---:|
| ORIGINAL | 351 | 30.2% | -62,765 | 0.78 | 77.8% |
| R | 157 | 36.9% | -22,805 | 0.83 | 35.0% |
| RR | 237 | 37.5% | -23,635 | 0.88 | 33.8% |
| RRR | 262 | 39.3% | -16,790 | 0.92 | 35.7% |

### M5_LONDON_ORB15_SKIP_NEWS_RR2p5

| variant | trades | win rate | net P&L | profit factor | max DD |
|---|---:|---:|---:|---:|---:|
| ORIGINAL | 402 | 24.6% | -61,713 | 0.74 | 70.0% |
| R | 107 | 36.5% | 37,249 | 1.74 | 4.5% |
| RR | 182 | 36.3% | 45,938 | 1.51 | 7.8% |
| RRR | 226 | 36.3% | 46,710 | 1.41 | 9.8% |

### M1_LONDON_ORB60_INCLUDE_NEWS_RR2

| variant | trades | win rate | net P&L | profit factor | max DD |
|---|---:|---:|---:|---:|---:|
| ORIGINAL | 427 | 31.9% | -61,375 | 0.80 | 78.1% |
| R | 158 | 36.7% | -11,465 | 0.91 | 25.3% |
| RR | 265 | 38.5% | 10,520 | 1.06 | 14.9% |
| RRR | 315 | 37.8% | -35 | 1.00 | 21.2% |

## Why this was re-simulated, not computed from the trade files

Flipping the sign of each trade's P&L in the CSV would have been wrong, and wrong
in the direction that flatters the reversal.

A losing trade exited at its stop: price moved 1R against it. The reversed trade
would have been 1R in profit at that moment — but its own target sits 2R away, and
whether price kept going or turned around and hit the reversed stop first is simply
not in the trade record. Sign-flipping assumes every reversed trade runs to target,
turning every -1R loss into a +2R win.

Measured on the first 19 pairs of one configuration: **zero were exact mirrors.**
The very first one is the clearest case —

```
01-02 03:45  original BUY  entry 4402.70  SL 4395.55  ->   -715
             reversed SELL entry 4402.70  SL 4409.85  ->   -715
```

Both lost. Price fell 1R (stopping the buy), then rose 1R (stopping the sell).
A sign flip would have reported +715 for the reversal instead of -715.
