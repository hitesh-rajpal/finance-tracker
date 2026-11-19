# 5paisa contract note reconciliation

Generated 2026-11-19 16:28 by `scripts/fivepaisa_bulk_recon.py`.

- Notes found: **429**
- Notes parsed: **427** over **422** dates
- Duplicate copies of the same note ignored: **2**
- Parse failures: **0**
- Filename/date mismatches: **0**
- Downloaded range: **2025-03-04 → 2026-11-17**
- Ledger: `Ledger_FY2020-21_to_2026-11-17.xlsx`

## Checks

| Check | Result |
|---|---|
| Ledger day totals | 422 days, **0 mismatch(es)**, **7 sign anomaly(ies)**, 0 day(s) with no bill |
| B/F continuity | 2778 links, **3 break(s)** |
| Closed positions explained | 83 positions, **0 unexplained**, 1 expired |

## Ledger mismatches and sign anomalies

A **sign anomaly** is an exact-magnitude, opposite-sign difference. `note_obligation` is the note's own Payin/Payout Obligation line: where it matches the ledger, the note's Net Amount line is the one with the wrong direction, so the amount itself is fine and only the note presentation is inconsistent.

```
      date  notes contract_nos  note_total  note_obligation  ledger_total      diff       status
2025-03-05      1          BSE   -97769.25        -97769.25      97769.25 -195538.5 SIGN ANOMALY
2025-03-12      1          BSE   -12099.00        -12099.00      12099.00  -24198.0 SIGN ANOMALY
2025-03-17      1          BSE   -37833.75        -37833.75      37833.75  -75667.5 SIGN ANOMALY
2025-03-18      1          BSE  -122779.00       -122779.00     122779.00 -245558.0 SIGN ANOMALY
2025-03-19      1          BSE   -65565.00        -65565.00      65565.00 -131130.0 SIGN ANOMALY
2025-03-20      1          BSE   -37938.50        -37938.50      37938.50  -75877.0 SIGN ANOMALY
2025-03-24      1          BSE  -141570.75       -141570.75     141570.75 -283141.5 SIGN ANOMALY
```

## B/F continuity breaks

Either an exchange price adjustment (most often an extraordinary dividend, per SEBI circular SEBI/HO/MRD2/MRD2_DCAP/P/CIR/2022/90) or a position that was closed and re-opened.

```
 prev_date       date                    contract side   qty  prev_close  cur_open  diff           status
2025-06-05 2025-06-06 FUTSTK BANKBARODA 26Jun2025    B  2925      251.31    242.96 -8.35 ADJUSTMENT/ERROR
2025-06-12 2025-06-13      FUTSTK CANBK 26Jun2025    B  6750      115.87    111.87 -4.00 ADJUSTMENT/ERROR
2026-02-12 2026-02-13       FUTSTK NMDC 24Feb2026    B 13500       84.91     82.41 -2.50 ADJUSTMENT/ERROR
```
