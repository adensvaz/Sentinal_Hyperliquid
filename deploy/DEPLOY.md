# Deploy systemd services

Copy a `.service` file and enable it:

```bash
sudo cp deploy/sentinel-carry.service           /etc/systemd/system/
sudo cp deploy/sentinel-carry-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel-carry sentinel-carry-dashboard
sudo systemctl status sentinel-carry sentinel-carry-dashboard --no-pager
```

Ports: **grid 8787** · champion 8788 · carry 8789 · trend 8790
Remember to open the GCP firewall for any new port.

---

## Grid book — replaces the retired Market-Neutral (takes port 8787, already open)

The market-neutral book was retired (cross-sectional-momentum edge decayed — negative the last two
years; Trend beat it on every robustness cut). The **grid** market-making book takes its port (8787),
so **no firewall change is needed**.

```bash
# 1. pull latest
cd ~/sentinel && git fetch origin && git reset --hard origin/main

# 2. STOP + DISABLE whatever currently serves 8787 (the old market-neutral loop + dashboard).
#    Find the unit names first, then disable them:
systemctl list-units 'sentinel*' --no-pager
#    e.g. (use YOUR actual neutral unit names):
# sudo systemctl disable --now sentinel sentinel-dashboard

# 3. install + start the grid book on 8787
sudo cp deploy/sentinel-grid.service           /etc/systemd/system/
sudo cp deploy/sentinel-grid-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel-grid sentinel-grid-dashboard
sudo systemctl status sentinel-grid sentinel-grid-dashboard --no-pager
```

Live lineup after this: **grid** (8787) · **champion** (8788) · **carry** (8789) · **trend** (8790).
All PAPER. The grid re-evaluates every 15 min; give it a few ticks to set anchors before it trades.
