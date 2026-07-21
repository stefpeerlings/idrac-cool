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

## Configuration

See `config.example.yaml`. Prefer secrets via environment:

| Variable | Purpose |
|----------|---------|
| `IDRAC_PASSWORD` | Shared iDRAC password |
| `IDRAC_USERNAME` | Shared iDRAC user (default `root`) |
| `DASHBOARD_USER` | First dashboard admin username |
| `DASHBOARD_PASSWORD` | First dashboard admin password |

Hosts can also be added in the UI (**+ iDRAC**).

## Security notes

- Do **not** expose port 8787 to the public internet without reverse proxy + HTTPS + strong passwords  
- iDRAC credentials may be stored in SQLite if you enter them in the UI — protect `data/`  
- Never commit `config.yaml` or `data/idrac.db`

## License

MIT — see [LICENSE](LICENSE).
