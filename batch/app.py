import asyncio
import datetime
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from http.client import HTTPException as HTTPClientError
from pathlib import Path
from typing import Any

from automation_engine import (
    BASE_DIR,
    CHROME_PATH,
    SCREENSHOTS_DIR,
    GoogleUltraAutomator,
    LocalHttpProxyAuthBridge,
    get_profile_dir,
    get_totp,
    parse_proxy_string,
    resolve_account_proxy,
)
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_LOGGER = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

app = FastAPI(title="Google AI Pro (5 TB) Two-Year Automation Console")
app.mount("/screenshots", StaticFiles(directory=SCREENSHOTS_DIR), name="screenshots")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Global runtime state
LOGS: list[str] = [
    f"[{time.strftime('%H:%M:%S')}] [SYSTEM] [INFO] Google AI Pro (5 TB) two-year automation console is ready (US$0.00 -> 2028-10)!"
]
ACTIVE_TASKS: dict[str, GoogleUltraAutomator] = {}
BATCH_RUNNING: bool = False
STOP_BATCH: bool = False
RESERVED_RUNS: dict[str, int] = {}
STOP_REQUESTED_ACCOUNTS: set[str] = set()


def format_display_date(value: str) -> str:
    value = (value or "").strip()
    numeric = re.fullmatch(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D*", value)
    if numeric:
        try:
            return datetime.date(*map(int, numeric.groups())).isoformat()
        except ValueError:
            return value
    for pattern in ("%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return value


def load_config() -> dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {"target_region": "US", "target_plan": "pro_2year_zero", "auto_cancel": True, "card": {}, "accounts": []}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    for account in cfg.get("accounts", []):
        if account.get("expiry_date"):
            account["expiry_date"] = format_display_date(account["expiry_date"])
    return cfg


def save_config(cfg: dict[str, Any]):
    temp_path = None
    try:
        # Write beside the target so replacement cannot expose a partial config.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=os.path.dirname(CONFIG_PATH),
            prefix=".config-", suffix=".tmp", delete=False,
        ) as f:
            temp_path = f.name
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, CONFIG_PATH)
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def append_log(line: str):
    print(line, flush=True)
    LOGS.append(line)
    if len(LOGS) > 500:
        del LOGS[: len(LOGS) - 500]


class ConfigUpdateReq(BaseModel):
    target_region: str = "US"
    target_plan: str = "pro_2year_zero"
    auto_cancel: bool = True
    proxy_template: str = ""
    card: dict[str, str]


class ImportAccountsReq(BaseModel):
    raw_text: str


class RunSingleReq(BaseModel):
    account_id: str
    start_step: int = 1


class RunBatchReq(BaseModel):
    account_ids: list[str] | None = None
    start_step: int = 1


class AccountActionReq(BaseModel):
    account_id: str


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(TEMPLATES_DIR) / "index.html"
    return await asyncio.to_thread(html_path.read_text, encoding="utf-8")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(os.path.join(STATIC_DIR, "favicon.ico"))


@app.get("/api/state")
async def get_state():
    cfg = load_config()
    rem_sec = 30 - (int(time.time()) % 30)
    accounts_out = []
    for acc in cfg.get("accounts", []):
        item = dict(acc)
        item["expiry_date"] = format_display_date(acc.get("expiry_date", "-"))
        try:
            item["totp_now"] = get_totp(acc.get("totp_secret", ""))
        except ValueError:
            _LOGGER.warning("Invalid TOTP secret for account %s", acc["id"])
            item["totp_now"] = "------"
        item["totp_rem"] = rem_sec
        item["is_running"] = acc["id"] in ACTIVE_TASKS or acc["id"] in RESERVED_RUNS
        accounts_out.append(item)

    return {
        "target_region": cfg.get("target_region", "US"),
        "target_plan": cfg.get("target_plan", "pro_2year_zero"),
        "auto_cancel": cfg.get("auto_cancel", True),
        "proxy_template": cfg.get("proxy_template", ""),
        "card": cfg.get("card", {}),
        "accounts": accounts_out,
        "logs": LOGS[-250:],
        "batch_running": BATCH_RUNNING,
        "active_count": len(set(ACTIVE_TASKS) | set(RESERVED_RUNS)),
    }


@app.post("/api/config")
async def update_config(req: ConfigUpdateReq):
    cfg = load_config()
    cfg["target_region"] = req.target_region
    cfg["target_plan"] = req.target_plan
    cfg["auto_cancel"] = req.auto_cancel
    cfg["proxy_template"] = req.proxy_template.strip()
    cfg["card"] = req.card
    save_config(cfg)
    append_log(f"[{time.strftime('%H:%M:%S')}] [CONFIG] [OK] Configuration saved (target region: {req.target_region} | proxy template: {'enabled' if req.proxy_template.strip() else 'system default'})")
    return {"ok": True}


class ProxyTestReq(BaseModel):
    target_region: str = "TR"
    proxy_template: str = ""


@app.post("/api/proxy/test")
async def test_proxy(req: ProxyTestReq):
    cfg = load_config()
    tpl = (req.proxy_template or cfg.get("proxy_template") or "").strip()
    region = (req.target_region or cfg.get("target_region") or "TR").upper()
    resolved = resolve_account_proxy({"id": f"probe{int(time.time())%1000}", "email": "probe@gmail.com"}, region, tpl)
    if not resolved:
        return {"ok": False, "message": "No proxy template provided"}

    def _sync_probe():
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": resolved, "https": resolved}))
        r = urllib.request.Request("https://ipwho.is/", headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(opener.open(r, timeout=15).read().decode("utf-8"))
        bal_gb = None
        api_key = cfg.get("floppy_api_key", "")
        if api_key:
            try:
                rb = urllib.request.Request(
                    "https://api.floppydata.net/v2/account/balances",
                    headers={"X-Api-Key": api_key, "User-Agent": "Mozilla/5.0", "Connection": "close"},
                )
                bdata = json.loads(urllib.request.urlopen(rb, timeout=10).read().decode("utf-8"))
                for key in ("proxy", "rotating", "total", "traffic"):
                    if not isinstance(bdata, dict):
                        raise TypeError("Proxy balance API returned a non-object response")
                    bdata = bdata.get(key, {})
                if not isinstance(bdata, dict):
                    raise TypeError("Proxy balance API returned invalid traffic data")
                bal_gb = bdata.get("availableGb")
            except (OSError, HTTPClientError, ValueError, TypeError):
                _LOGGER.warning("Proxy balance lookup failed", exc_info=True)
        return data, bal_gb

    try:
        data, bal_gb = await asyncio.to_thread(_sync_probe)
        if not isinstance(data, dict):
            raise TypeError("IP probe returned a non-object response")
        ip = data.get("ip", "Unknown")
        cc = data.get("country_code", "??")
        country = data.get("country", "")
        city = data.get("city", "")
        connection = data.get("connection") or {}
        if not isinstance(connection, dict):
            raise TypeError("IP probe returned invalid connection data")
        org = connection.get("org", "")
        flag = cc
        bal_str = f" | FloppyData remaining traffic: {bal_gb} GB" if bal_gb is not None else ""
        msg = f"{flag} Connection successful! Exit IP: {ip} ({country} · {city} | {org}){bal_str}"
        append_log(f"[{time.strftime('%H:%M:%S')}] [PROXY] [OK] {msg}")
        return {"ok": True, "message": msg, "ip": ip, "country_code": cc, "balance_gb": bal_gb}
    except (OSError, HTTPClientError, ValueError, TypeError) as e:
        err = f"Proxy test failed: {e}"
        append_log(f"[{time.strftime('%H:%M:%S')}] [PROXY] [ERROR] {err}")
        return {"ok": False, "message": err}


@app.post("/api/accounts/import")
async def import_accounts(req: ImportAccountsReq):
    cfg = load_config()
    existing = cfg.get("accounts", [])
    existing_emails = {a["email"].lower().replace(".", "") for a in existing}

    lines = [l.strip() for l in req.raw_text.splitlines() if l.strip()]
    added = 0
    for line in lines:
        # Support delimiters: ----, —, |, /, \t, comma, or whitespace
        if "----" in line:
            parts = [p.strip() for p in line.split("----") if p.strip()]
        elif "—" in line:
            parts = [p.strip() for p in line.split("—") if p.strip()]
        elif "|" in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
        elif "/" in line and "@" in line and "http://" not in line and "https://" not in line:
            parts = [p.strip() for p in line.split("/") if p.strip()]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t") if p.strip()]
        elif "," in line:
            parts = [p.strip() for p in line.split(",") if p.strip()]
        else:
            parts = [p.strip() for p in re.split(r"\s+", line) if p.strip()]

        if len(parts) < 2:
            continue

        # Detect if first part is a label (no '@') or email (has '@')
        if "@" in parts[0]:
            email = parts[0]
            password = parts[1]
            rest = parts[2:]
            number = len(existing) + 1
            used_ids = {a["id"] for a in existing}
            while f"Account_{number}" in used_ids:
                number += 1
            acc_id = f"Account_{number}"
        elif len(parts) >= 3 and "@" in parts[1]:
            acc_id = parts[0]
            email = parts[1]
            password = parts[2]
            rest = parts[3:]
        else:
            continue

        recovery_email = ""
        totp_secret = ""
        proxy_str = ""
        for token in rest:
            t_clean = token.replace(" ", "")
            if "@" in token and not token.lower().startswith(("http://", "https://", "socks5://")):
                if token.lower() != email.lower() and not recovery_email:
                    recovery_email = token.strip()
            elif token.lower().startswith(("http://", "https://", "socks5://")) or re.match(r"^\d+\.\d+\.\d+\.\d+:\d+", token):
                proxy_str = token.strip()
            elif len(t_clean) >= 16 and re.match(r"^[A-Za-z2-7]+=*$", t_clean) and not totp_secret:
                totp_secret = t_clean

        norm_email = email.lower().replace(".", "")
        if norm_email in existing_emails:
            for a in existing:
                if a["email"].lower().replace(".", "") == norm_email:
                    a["password"] = password
                    if recovery_email:
                        a["recovery_email"] = recovery_email
                    if totp_secret:
                        a["totp_secret"] = totp_secret
                    if proxy_str:
                        a["proxy"] = proxy_str
            continue

        if any(a["id"] == acc_id for a in existing):
            raise HTTPException(status_code=400, detail=f"Account label already exists: {acc_id}")

        existing.append(
            {
                "id": acc_id,
                "email": email,
                "password": password,
                "recovery_email": recovery_email,
                "totp_secret": totp_secret,
                "proxy": proxy_str,
                "ip_info": "",
                "status": "pending",
                "current_step": 0,
                "step_message": "Newly imported account (pending)",
                "expiry_date": "-",
                "last_screenshot": "",
            }
        )
        existing_emails.add(norm_email)
        added += 1

    cfg["accounts"] = existing
    save_config(cfg)
    append_log(f"[{time.strftime('%H:%M:%S')}] [IMPORT] [OK] Imported/updated {added} accounts ({len(existing)} total)")
    return {"ok": True, "added": added, "total": len(existing)}


@app.post("/api/accounts/delete")
async def delete_account(req: AccountActionReq):
    if req.account_id in ACTIVE_TASKS or req.account_id in RESERVED_RUNS:
        raise HTTPException(status_code=400, detail="This account is running. Stop the task first.")
    cfg = load_config()
    cfg["accounts"] = [a for a in cfg.get("accounts", []) if a["id"] != req.account_id]
    save_config(cfg)
    append_log(f"[{time.strftime('%H:%M:%S')}] [ACCOUNT] [INFO] Removed account {req.account_id}")
    return {"ok": True}


@app.post("/api/accounts/reset")
async def reset_account(req: AccountActionReq):
    cfg = load_config()
    for a in cfg.get("accounts", []):
        if a["id"] == req.account_id:
            a["status"] = "pending"
            a["current_step"] = 0
            a["step_message"] = "Status reset (pending)"
            break
    save_config(cfg)
    append_log(f"[{time.strftime('%H:%M:%S')}] [ACCOUNT] [INFO] Reset account {req.account_id} to pending")
    return {"ok": True}


async def _run_account_worker(account_id: str, start_step: int = 1, port: int = 9320):
    import importlib

    import automation_engine
    importlib.reload(automation_engine)
    cfg = load_config()
    target_acc = None
    for a in cfg.get("accounts", []):
        if a["id"] == account_id:
            target_acc = a
            break
    if not target_acc:
        return

    def on_state_patch(patch: dict[str, Any]):
        c = load_config()
        for acc_item in c.get("accounts", []):
            if acc_item["id"] == account_id:
                acc_item.update(patch)
                break
        save_config(c)

    automator = automation_engine.GoogleUltraAutomator(
        account=target_acc,
        card=cfg.get("card", {}),
        target_region=cfg.get("target_region", "US"),
        target_plan=cfg.get("target_plan", "pro_2year_zero"),
        auto_cancel=cfg.get("auto_cancel", True),
        proxy_template=cfg.get("proxy_template", ""),
        port=port,
        on_log=append_log,
        on_state=on_state_patch,
    )
    ACTIVE_TASKS[account_id] = automator
    if account_id in STOP_REQUESTED_ACCOUNTS:
        automator.stop_requested = True
        STOP_REQUESTED_ACCOUNTS.discard(account_id)
    try:
        if automator.stop_requested:
            automator.update_state(status="stopped", step_message="Stopped before starting")
            return
        await automator.run_pipeline(start_step=start_step)
    finally:
        ACTIVE_TASKS.pop(account_id, None)


def _reserve_run(account_id: str) -> int:
    if account_id in ACTIVE_TASKS or account_id in RESERVED_RUNS:
        raise HTTPException(status_code=400, detail="This account is already running")
    if not any(a["id"] == account_id for a in load_config().get("accounts", [])):
        raise HTTPException(status_code=404, detail="Account not found")
    used_ports = set(RESERVED_RUNS.values()) | {
        task.port for task in ACTIVE_TASKS.values()
    }
    port = next((p for p in range(9320, 9420) if p not in used_ports), None)
    if port is None:
        raise HTTPException(status_code=400, detail="No automation ports available")
    RESERVED_RUNS[account_id] = port
    return port


async def _run_reserved_account(account_id: str, start_step: int, port: int):
    try:
        await _run_account_worker(account_id, start_step=start_step, port=port)
    finally:
        RESERVED_RUNS.pop(account_id, None)
        STOP_REQUESTED_ACCOUNTS.discard(account_id)


@app.post("/api/run/single")
async def run_single(req: RunSingleReq):
    port = _reserve_run(req.account_id)
    asyncio.create_task(_run_reserved_account(req.account_id, req.start_step, port))
    return {"ok": True}


@app.post("/api/run/batch")
async def run_batch(req: RunBatchReq):
    global BATCH_RUNNING, STOP_BATCH
    if BATCH_RUNNING:
        raise HTTPException(status_code=400, detail="The batch queue is already running")

    cfg = load_config()
    accounts = {a["id"]: a for a in cfg.get("accounts", [])}
    busy = set(ACTIVE_TASKS) | set(RESERVED_RUNS)
    if req.account_ids:
        queue_ids = list(dict.fromkeys(req.account_ids))
        if any(aid not in accounts for aid in queue_ids):
            raise HTTPException(status_code=404, detail="Account not found")
        if any(aid in busy for aid in queue_ids):
            raise HTTPException(status_code=400, detail="A selected account is already running")
    else:
        queue_ids = [aid for aid, a in accounts.items()
                     if a.get("status") != "completed" and aid not in busy]

    if not queue_ids:
        raise HTTPException(status_code=400, detail="No idle pending accounts are available.")

    BATCH_RUNNING = True
    STOP_BATCH = False

    async def _batch_runner():
        global BATCH_RUNNING
        append_log(f"[{time.strftime('%H:%M:%S')}] [BATCH] [INFO] Starting batch queue: {queue_ids} (starting at Step {req.start_step})")
        try:
            for aid in queue_ids:
                if STOP_BATCH:
                    append_log(f"[{time.strftime('%H:%M:%S')}] [BATCH] [WARN] Batch queue stopped by user")
                    break
                if aid in ACTIVE_TASKS or aid in RESERVED_RUNS:
                    continue
                # Accounts can be removed while waiting in the queue.
                if not any(a["id"] == aid for a in load_config().get("accounts", [])):
                    continue
                port = _reserve_run(aid)
                await _run_reserved_account(aid, req.start_step, port)
                await asyncio.sleep(2)
        finally:
            BATCH_RUNNING = False
            append_log(f"[{time.strftime('%H:%M:%S')}] [BATCH] [OK] Batch queue finished")

    asyncio.create_task(_batch_runner())
    return {"ok": True, "queued": queue_ids}


@app.post("/api/stop")
async def stop_all(req: AccountActionReq | None = None):
    global STOP_BATCH
    STOP_BATCH = True
    if req and req.account_id:
        if req.account_id in RESERVED_RUNS:
            STOP_REQUESTED_ACCOUNTS.add(req.account_id)
        if req.account_id in ACTIVE_TASKS:
            ACTIVE_TASKS[req.account_id].stop_requested = True
        append_log(f"[{time.strftime('%H:%M:%S')}] [STOP] [WARN] Stopping account {req.account_id}...")
    else:
        STOP_REQUESTED_ACCOUNTS.update(RESERVED_RUNS)
        for task in list(ACTIVE_TASKS.values()):
            task.stop_requested = True
        append_log(f"[{time.strftime('%H:%M:%S')}] [STOP] [WARN] Global stop requested")
    return {"ok": True}


MANUAL_BRIDGES: dict[str, LocalHttpProxyAuthBridge] = {}


def _launch_browser(args: list[str]):
    subprocess.Popen(args, start_new_session=True)


@app.post("/api/open_browser")
async def open_browser_manual(req: AccountActionReq):
    cfg = load_config()
    target_acc = next((a for a in cfg.get("accounts", []) if a["id"] == req.account_id), None)
    if not target_acc:
        raise HTTPException(status_code=404, detail="Account not found")

    user_data_dir = get_profile_dir(target_acc["id"], target_acc["email"])
    target_region = cfg.get("target_region", "US")
    proxy_template = cfg.get("proxy_template", "")
    resolved_proxy = resolve_account_proxy(target_acc, target_region, proxy_template)
    parsed_proxy = parse_proxy_string(resolved_proxy)

    chrome_args = [
        CHROME_PATH,
        f"--user-data-dir={user_data_dir}",
        "--lang=en-US",
        "--no-first-run",
        "--no-default-browser-check",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--enforce-webrtc-ip-permission-check",
        "--start-maximized",
    ]

    if parsed_proxy:
        if parsed_proxy.get("username"):
            if req.account_id in MANUAL_BRIDGES:
                try:
                    await MANUAL_BRIDGES[req.account_id].stop()
                except OSError:
                    _LOGGER.warning(
                        "Failed to stop manual proxy bridge for account %s",
                        req.account_id,
                        exc_info=True,
                    )
            acc_idx = sum(ord(c) for c in req.account_id) % 50
            bridge_port = 9600 + acc_idx
            bridge = LocalHttpProxyAuthBridge(bridge_port, parsed_proxy)
            await bridge.start()
            MANUAL_BRIDGES[req.account_id] = bridge
            chrome_args.append(f"--proxy-server=http://127.0.0.1:{bridge_port}")
        else:
            p_url = f"{parsed_proxy['scheme']}://{parsed_proxy['host']}:{parsed_proxy['port']}"
            chrome_args.append(f"--proxy-server={p_url}")

    chrome_args.extend(
        [
            "https://one.google.com/plans?hl=en-US",
            "https://play.google.com/store/account/subscriptions?hl=en-US",
            "https://play.google.com/store/account/orderhistory?hl=en-US",
        ]
    )
    _launch_browser(chrome_args)
    append_log(
        f"[{time.strftime('%H:%M:%S')}] [BROWSER] [INFO] Opened a dedicated Chrome window for [{target_acc['id']}] ({target_acc['email']})"
    )
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
