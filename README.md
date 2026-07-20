# iDRAC Cool

Web dashboard for Dell PowerEdge servers (iDRAC / IPMI): live temperatures, fan control, multi-host, and a small login system.

**Auto** = smart fan curve (capped, typically 30%) · **Manual** = preset + custom % · **Dell Auto** = firmware control.

## Features

- Live **inlet / outlet / CPU 1 & 2** + fan RPM  
- Per host: **Auto / Manual / Dell Auto**  
- Fan presets **15–50%** + **custom %**  
- Multi-iDRAC panels (add / edit / detect)  
- State after reboot via re-apply loop  
- Login, accounts, language (NL/EN)  
- SQLite storage (`data/idrac.db`)

## Requirements

- Python 3.11+  
- [`ipmitool`](https://github.com/ipmitool/ipmitool) (`apt install ipmitool`)  
- Network access to iDRAC (**UDP 623**)  
- iDRAC user with IPMI privileges  

## Quick start

```bash
git clone https://github.com/stefpeerlings/idrac-cool.git
cd idrac-cool

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# optional: edit hosts / bind port

export IDRAC_PASSWORD='your-idrac-password'
# optional first-run dashboard account:
# export DASHBOARD_USER=admin
# export DASHBOARD_PASSWORD='change-me'

python -m uvicorn app.main:app --host 0.0.0.0 --port 8787
```

Open: **http://localhost:8787**

Default dashboard login (first start only):

| | |
|--|--|
| User | `admin` |
| Password | `admin123` |

Change this immediately under **Settings → My account**.

### Live helper script

```bash
export IDRAC_PASSWORD='***'
./run-live.sh
```

### Mock mode (no hardware)

```bash
export MOCK_IPMI=1
./run.sh
```

## Deploy

Works well on:

- **Raspberry Pi OS Lite** (headless)  
- **Proxmox LXC** (Debian/Ubuntu CT)  
- Any always-on Linux host  

Open from another PC (like the Proxmox UI):

```text
http://<host-ip>:8787
```

---

### Raspberry Pi (headless / Lite)

No desktop required. The dashboard runs as a service; you open the UI in a browser on another PC.

#### What you need

| Item | Recommendation |
|------|----------------|
| Board | Pi 3 / 4 / 5 (Zero 2W works, but slower) |
| OS | **Raspberry Pi OS Lite** 64-bit |
| Storage | SD card (USB SSD better for 24/7) |
| Network | Ethernet preferred; same LAN/VLAN as iDRAC |
| Packages | `python3`, `python3-venv`, `python3-pip`, `ipmitool`, `git` |

#### Install

```bash
# SSH into the Pi
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ipmitool git

git clone https://github.com/stefpeerlings/idrac-cool.git
cd idrac-cool

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml

export IDRAC_PASSWORD='your-idrac-password'
# optional:
# export IDRAC_USERNAME=root
# export DASHBOARD_PASSWORD='strong-password'

./run-live.sh
```

From your PC:

```text
http://<pi-ip>:8787
```

#### Always-on (systemd)

```bash
sudo mkdir -p /opt/idrac-cool
sudo cp -a . /opt/idrac-cool/
sudo useradd -r -s /usr/sbin/nologin idraccool || true
sudo chown -R idraccool:idraccool /opt/idrac-cool

# Edit paths/user/password in the unit file first
sudo cp idrac-dashboard.service /etc/systemd/system/idrac-cool.service
sudo nano /etc/systemd/system/idrac-cool.service

sudo systemctl daemon-reload
sudo systemctl enable --now idrac-cool
sudo systemctl status idrac-cool
```

Tip: give the Pi a **DHCP reservation** or static IP so the URL stays the same.

---

### Proxmox LXC

Same model as Proxmox itself: the app runs in a container; you log in from a browser on another PC.

#### Create the CT — recommended settings

In the Proxmox UI: **Create CT**

| Setting | Recommended value | Notes |
|---------|-------------------|--------|
| **General → Hostname** | `idrac-cool` | |
| **General → Unprivileged container** | **Yes** | Enough for IPMI-over-LAN |
| **General → Nesting** | No | Not required |
| **Template** | **Debian 12** or **Ubuntu 22.04/24.04** | Standard template |
| **Disk** | **4–8 GB** | App is small; room for SQLite + logs |
| **Storage** | your local/ZFS pool | |
| **CPU cores** | **1** (or 2) | Light load |
| **Memory** | **512 MB** (1024 MB comfortable) | |
| **Swap** | **512 MB** | |
| **Network → Bridge** | `vmbr0` (or your LAN bridge) | Must reach iDRAC subnet |
| **Network → IPv4** | DHCP or static | Static/reservation recommended |
| **Network → Firewall** | optional | Allow TCP **8787** from LAN if enabled |
| **DNS** | host / your LAN DNS | |

**Network note:** the CT must reach the iDRAC management IPs (UDP **623**).  
If iDRAC is on a management VLAN, attach the CT to that bridge/VLAN or add a route.

#### Install inside the CT

```bash
# From Proxmox host (use your CT ID)
pct enter <CTID>
# or: ssh into the CT

apt update
apt install -y python3 python3-venv python3-pip ipmitool git curl

git clone https://github.com/stefpeerlings/idrac-cool.git /opt/idrac-cool
cd /opt/idrac-cool

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml

# Create env file for secrets
cat >/etc/idrac-cool.env <<'EOF'
IDRAC_PASSWORD=your-idrac-password
# IDRAC_USERNAME=root
# DASHBOARD_PASSWORD=strong-password
EOF
chmod 600 /etc/idrac-cool.env
```

#### Systemd in the CT

```bash
cat >/etc/systemd/system/idrac-cool.service <<'EOF'
[Unit]
Description=iDRAC Cool Fan Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/idrac-cool
EnvironmentFile=/etc/idrac-cool.env
Environment=PATH=/opt/idrac-cool/.venv/bin:/usr/bin
ExecStart=/opt/idrac-cool/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8787
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now idrac-cool
systemctl status idrac-cool
```

From your PC:

```text
http://<lxc-ip>:8787
```

#### Quick checks in the CT

```bash
# Can we reach iDRAC?
ping -c 2 10.0.30.6

# IPMI over LAN?
ipmitool -I lanplus -H 10.0.30.6 -U root -P "$IDRAC_PASSWORD" mc info
```

#### Backup

Panels, users and fan state live in:

```text
/opt/idrac-cool/data/idrac.db
```

Include that path in your CT/PBS backups.

---

### Pi vs LXC

| | Raspberry Pi | Proxmox LXC |
|--|--------------|-------------|
| Always-on | Separate device | Runs when the Proxmox host is on |
| Power | Very low | Uses the server |
| Management | SSH only | Same as your other CTs |
| Best for | Isolated mini appliance | Homelab next to other services |

## Configuration

See `config.example.yaml`. Prefer secrets via environment:

| Variable | Purpose |
|----------|---------|
| `IDRAC_PASSWORD` | Shared iDRAC password |
| `IDRAC_USERNAME` | Shared iDRAC user (default `root`) |
| `DASHBOARD_USER` | First dashboard admin username |
| `DASHBOARD_PASSWORD` | First dashboard admin password |
| `MOCK_IPMI` | `1` = fake sensors (dev) |

Hosts can also be added in the UI (**+ iDRAC**).

## Security notes

- Do **not** expose port 8787 to the public internet without reverse proxy + HTTPS + strong passwords  
- iDRAC credentials may be stored in SQLite if you enter them in the UI — protect `data/`  
- Never commit `config.yaml` or `data/idrac.db`

## License

MIT — see [LICENSE](LICENSE).
