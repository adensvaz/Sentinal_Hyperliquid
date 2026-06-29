# Deploy systemd services

Copy a `.service` file and enable it:

```bash
sudo cp deploy/sentinel-carry.service           /etc/systemd/system/
sudo cp deploy/sentinel-carry-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel-carry sentinel-carry-dashboard
sudo systemctl status sentinel-carry sentinel-carry-dashboard --no-pager
```

Ports: neutral 8787 · champion 8788 · carry 8789
Remember to open the GCP firewall for any new port.
