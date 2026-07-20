"""Treasury — the money manager that sweeps profit into a safe Vault on a schedule.

Every `sweep_interval_days`, if the trading pool is at a NEW high, it moves `sweep_frac` of the
new profit into the Vault. The Vault is safe (not traded) — for withdrawal or reinvestment.
High-water-mark based, so it never sweeps a down or flat period (no over-sweeping in chop).

State (persisted per book in the `treasury` table):
  vault          — cumulative swept amount held safe
  hwm            — high-water mark of the trading pool (post-last-sweep baseline)
  last_sweep_ts  — unix time of the last weekly checkpoint
  total_swept    — lifetime total moved to the vault (== vault unless money is withdrawn)
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class SweepResult:
    vault: float          # new vault balance
    hwm: float            # new high-water mark (post-sweep trading level)
    last_sweep_ts: float  # new checkpoint time
    swept: float          # amount moved to the vault this call (0 if none)
    checkpoint: bool      # True if a weekly checkpoint fired (whether or not it swept)


class Treasury:
    def __init__(self, cfg):
        self.c = cfg.treasury          # TreasuryCfg

    def maybe_sweep(self, trading_equity: float, vault: float, hwm: float,
                    last_sweep_ts: float, now: float) -> SweepResult:
        """Decide whether to sweep at time `now`.

        Parameters
        ----------
        trading_equity : the at-risk pool right now  (= broker equity - vault)
        vault, hwm, last_sweep_ts : persisted treasury state
        now : current unix time

        Returns a SweepResult with the new state and the amount swept (0 if none).
        """
        # disabled -> no-op, but keep the hwm initialised so it's ready if turned on later
        if not self.c.enabled:
            return SweepResult(vault, hwm if hwm > 0 else trading_equity, last_sweep_ts, 0.0, False)

        # first ever call: seed the high-water mark and the clock
        if hwm <= 0:
            hwm = trading_equity
        if last_sweep_ts <= 0:
            last_sweep_ts = now

        due = (now - last_sweep_ts) >= self.c.sweep_interval_days * 86400.0
        if not due:
            return SweepResult(vault, hwm, last_sweep_ts, 0.0, False)

        # weekly checkpoint reached
        if trading_equity > hwm:
            gain = trading_equity - hwm
            swept = max(0.0, self.c.sweep_frac) * gain
            new_vault = vault + swept
            new_hwm = trading_equity - swept          # post-sweep trading level becomes the new baseline
            return SweepResult(new_vault, new_hwm, now, swept, True)

        # down / flat week -> bank nothing, just reset the clock (baseline unchanged)
        return SweepResult(vault, hwm, now, 0.0, True)
