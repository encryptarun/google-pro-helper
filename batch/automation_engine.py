import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import socket
import struct
import subprocess
import time
from collections.abc import Callable
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

_LOGGER = logging.getLogger(__name__)

_CHECKOUT_MONEY = re.compile(
    r"(?<![\w.])(?:US\$|\$|€|£|₹|₺|USD\s*|INR\s*|TRY\s*)(\d+(?:,\d{3})*(?:\.\d{2})?)(?![\d.])",
    re.IGNORECASE,
)
_DUE_NOW_LABEL = re.compile(
    r"\b(?:due\s+(?:today|now)|(?:today|now)(?:'s)?\s+(?:total|due|payment|charge|amount)|(?:total|pay|charged?|amount\s+due)\s+today)\b",
    re.IGNORECASE,
)
_FUTURE_CHARGE_LABEL = re.compile(
    r"\b(?:after\s+(?:the\s+)?trial|afterwards|then|renews?|renewal|next\s+(?:payment|billing)|starting\s+on)\b",
    re.IGNORECASE,
)


def _checkout_due_now_is_zero(dialog_text: str) -> bool:
    """Accept only an explicitly labelled zero amount payable now."""
    parts = [part.strip() for part in dialog_text.split("|") if part.strip()]
    due_amounts = []
    for index, part in enumerate(parts):
        due_label = _DUE_NOW_LABEL.search(part) or re.fullmatch(
            r"today", part, re.IGNORECASE
        )
        future_label = _FUTURE_CHARGE_LABEL.search(part)
        amounts = list(_CHECKOUT_MONEY.finditer(part))
        if due_label:
            due_amounts.extend(
                match.group(1)
                for match in amounts
                if not future_label or match.start() < future_label.start()
            )
            if not amounts and index + 1 < len(parts):
                due_amounts.extend(_CHECKOUT_MONEY.findall(parts[index + 1]))
        for match in amounts:
            if match.group(1).replace(",", "") in ("0", "0.00"):
                continue
            if due_label and (not future_label or match.start() < future_label.start()):
                return False
            if not future_label:
                return False
            if match.start() < future_label.start():
                suffix_gap = part[match.end():future_label.start()]
                clear_trial_price = (
                    not part[:match.start()].strip()
                    and re.fullmatch(r"after\s+(?:the\s+)?trial", future_label.group(0), re.IGNORECASE)
                    and re.fullmatch(
                        r"\s*(?:/(?:month|mo|year|yr)|per\s+(?:month|year))?\s*",
                        suffix_gap,
                        re.IGNORECASE,
                    )
                )
                if not clear_trial_price:
                    return False
            if re.search(r"\btoday\b", part, re.IGNORECASE) and match.start() < future_label.start():
                return False
    return bool(due_amounts) and all(
        amount.replace(",", "") in ("0", "0.00") for amount in due_amounts
    )


_MONTHS = {name: number for number, names in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"),
    ("apr", "april"), ("may",), ("jun", "june"),
    ("jul", "july"), ("aug", "august"), ("sep", "september"),
    ("oct", "october"), ("nov", "november"), ("dec", "december"),
), 1) for name in names}
_MONTH_NAME = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
_DATE_TEXT = (
    rf"(?:[0-9]{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_NAME}\s*,?\s*[0-9]{{4}}"
    rf"|{_MONTH_NAME}\s+[0-9]{{1,2}}(?:st|nd|rd|th)?\s*,?\s*[0-9]{{4}}"
    r"|[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})"
)
_EXPIRY_LABEL = re.compile(
    rf"\b(?:renews?\s+on|renewal\s+date|next\s+payment(?:\s+on)?|"
    rf"expires?\s+on|expiry\s+date|ends?\s+on|end\s+date|"
    rf"access\s+until|subscription\s+ends?\s+on)\s*:?\s*({_DATE_TEXT})",
    re.IGNORECASE,
)


def _parse_english_date(value: str) -> datetime.date:
    value = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", value.strip(), flags=re.IGNORECASE)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return datetime.date.fromisoformat(value)
    if re.fullmatch(r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}", value):
        month, day, year = map(int, value.split("/"))
        return datetime.date(year, month, day)
    parts = value.replace(",", "").split()
    if len(parts) != 3:
        raise ValueError("Unrecognised subscription date")
    if parts[0].isdigit():
        day, month_name, year = parts
    else:
        month_name, day, year = parts
    month_name = month_name.lower()
    if month_name == "sept":
        month_name = "sep"
    return datetime.date(int(year), _MONTHS[month_name], int(day))


def _subscription_expiry_date(text: str) -> datetime.date:
    matches = list(_EXPIRY_LABEL.finditer(text))
    if not matches:
        raise RuntimeError("Final verification could not identify one subscription expiration date")
    try:
        dates = {_parse_english_date(match.group(1)) for match in matches}
    except (ValueError, KeyError) as exc:
        raise RuntimeError("Final verification found an invalid expiration date") from exc
    if len(dates) != 1:
        raise RuntimeError("Final verification could not identify one subscription expiration date")
    expiry = dates.pop()
    if expiry <= datetime.date.today():
        raise RuntimeError("Final verification found a subscription date that has passed")
    return expiry


def _find_chrome_path() -> str:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expanduser(
            "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        ),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(
        "Google Chrome was not found. Install it in /Applications or ~/Applications."
    )


def _stop_processes_listening_on_port(port: int) -> None:
    """Stop stale local Chrome processes that still own a debugging port."""
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return

    for value in result.stdout.splitlines():
        try:
            os.kill(int(value), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, ValueError):
            continue


async def _stop_process_group(process: asyncio.subprocess.Process) -> None:
    """Stop Chrome and reap its process without blocking the event loop."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            _LOGGER.debug("Chrome process group exited before forced cleanup")
        await process.wait()


CHROME_PATH = _find_chrome_path()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(BASE_DIR, "profiles")
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "screenshots")

os.makedirs(PROFILES_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Google One Offer Tokens (Universal across accounts/currencies)
TOK_PRO5TB_12M = "AVbCH/cspB8VdqKJ+ax/uPxUFAVuYRPX4iSjtg4Fu0PG9zUeURSM7y//LlQva1dCd/rbKVM6EJmyiG/C9BoQ95pMl8yDCHuiwCvM5CuGJME="
TOK_PRO5TB_6M = "AVbCH/fUON3w8hpfeutprA3BMQwrBwYIB6Y8CSWi1isDCDswKUUhGQ3dluj5WXsNKOvr8K5UULBGrRsf4KlW1rHLz0fF3SvxQhBFRbBOHCA="
TOK_PRO5TB_6M_2 = "AVbCH/cJ5SeHgkVRt+bdcrapx/aLSu2kbsBgMdI413zbllDNsNQoC/zVlG/Npbu55WlMzBwWPhUedIvZFDg7fw0CmCv/8DKj04IZ21pK7mrV/hh5"
TOK_PRO5TB_6M_SS = "AVbCH/cgl4zvPT2Lqw95Gn+RYAJIVwFwi783i6uVprX7u3MCa5J4CcTXH5RABa5WjBrUoQnwf8vI10TNV7ktd26myIdxqIwGmNoJ5ofYNqpQ10RKoSvHs75wsfA="
TOK_P1M = "AVbCH/dXbuvFhoeuGZb/+3+b5Jhft4kOY/wQLGqLhy0hhx6hDAJ/1z9y23+x9N5zyNtQ268DEw=="
TOK_MONTHLY = "AVbCH/dSbs1UHItdkpohleJ9+69eSPWyFRutIpRUdGPlsdDKw2IMvRRHiwDefRVyEHl12jqfmBgQZByeX7hJ"
YT_FREE_TOK = "AVbCH/eW4Nfp8KBl0zlm7x9PhaiCvpnqwUuL2D8NFPBQgmM9+HuYS1h2xH5lLWU/BsBj775LK5GqK8UHKqoSzhDDz+PqP3paNesqLU0r9i6XCrYa"
PREFIX = "subs:com.google.android.apps.subscriptions.red:"

HOOK_JS = """
(() => {
  if (!location.hostname.includes('one.google.com')) return;
  if (window.__HOOK_INSTALLED) return;
  window.__HOOK_INSTALLED = true;
  let currentTest = window.__INIT_TEST || null;
  window.__setDirectTest = (t) => { currentTest = t; };

  function patchTargetNode(node) {
    if (!Array.isArray(node) || !Array.isArray(currentTest?.full_items)) return false;
    let patched = false;
    if (node.length > 12 && Array.isArray(node[12])) {
      if (Array.isArray(node[7]) && Array.isArray(node[7][5]) && node[7][5].length > 0) {
        window.__capturedSubId = node[7][5][0];
      }
      node[12] = currentTest.full_items;
      const targetMode = currentTest.mode || (Array.isArray(currentTest.ear8) ? currentTest.ear8[1] : 5) || 5;
      if (currentTest.ear8) {
        node[7] = currentTest.ear8;
      } else if (Array.isArray(node[7]) && Array.isArray(node[7][5]) && node[7][5].length > 0) {
        node[7][1] = targetMode;
      } else if (window.__capturedSubId) {
        node[7] = [null, targetMode, null, null, null, [window.__capturedSubId]];
      }
      patched = true;
    }
    if (node.length > 10 && Array.isArray(node[10]) && Array.isArray(node[10][0])) {
      node[10] = [currentTest.full_items];
      if (Array.isArray(node[2]) && Array.isArray(node[2][2]) && Array.isArray(node[2][2][8])) {
        node[2][2][8] = [currentTest.full_items];
      }
      if (currentTest.ear8) {
        node[6] = currentTest.ear8;
      }
      patched = true;
    }
    return patched;
  }

  function rewriteFreq(bodyStr) {
    if (typeof bodyStr !== 'string' || !bodyStr.includes('f.req=') || !currentTest) return bodyStr;
    try {
      const parts = bodyStr.split('&');
      for (let i = 0; i < parts.length; i++) {
        if (parts[i].startsWith('f.req=')) {
          const raw = decodeURIComponent(parts[i].slice(6).replace(/\\+/g, ' '));
          const outer = JSON.parse(raw);
          let changed = false;
          if (Array.isArray(outer)) {
            for (const group of outer) {
              if (!Array.isArray(group)) continue;
              for (const call of group) {
                if (Array.isArray(call) && call[0] === 'Ngbm' && typeof call[1] === 'string') {
                  const payload = JSON.parse(call[1]);
                  if (Array.isArray(payload)) {
                    if (patchTargetNode(payload[0])) changed = true;
                    if (patchTargetNode(payload[1])) changed = true;
                  }
                  if (changed) {
                    call[1] = JSON.stringify(payload);
                  }
                }
              }
            }
          }
          if (changed) {
            parts[i] = 'f.req=' + encodeURIComponent(JSON.stringify(outer));
            return parts.join('&');
          }
        }
      }
    } catch (e) {}
    return bodyStr;
  }

  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(body) {
    if (typeof body === 'string' && body.includes('Ngbm')) {
      body = rewriteFreq(body);
    }
    return origSend.call(this, body);
  };

  function rewriteUrl(val) {
    const s = String(val || '');
    if (!s.includes('/eacquire/multiline') || !currentTest) return val;
    try {
      const u = new URL(s, location.origin);
      const earStr = u.searchParams.get('ear');
      if (!earStr) return val;
      const ear = JSON.parse(earStr);
      if (Array.isArray(ear[8]) && Array.isArray(ear[8][5]) && ear[8][5].length > 0) {
        window.__capturedSubId = ear[8][5][0];
      }
      if (Array.isArray(ear[13]) && Array.isArray(currentTest.full_items)) {
        ear[13][0] = currentTest.full_items;
      }
      if (Array.isArray(ear[4]) && typeof ear[4][0] === 'string' && Array.isArray(currentTest.full_items)) {
        try {
          const inner4 = JSON.parse(atob(ear[4][0]));
          if (Array.isArray(inner4) && inner4.length > 8) {
            inner4[8] = [currentTest.full_items];
            ear[4][0] = btoa(JSON.stringify(inner4));
          }
        } catch (e4) {}
      }
      const targetMode = currentTest.mode || (Array.isArray(currentTest.ear8) ? currentTest.ear8[1] : 5) || 5;
      if (currentTest.ear8) {
        ear[8] = currentTest.ear8;
      } else if (Array.isArray(ear[8]) && Array.isArray(ear[8][5]) && ear[8][5].length > 0) {
        ear[8][1] = targetMode;
      } else if (window.__capturedSubId) {
        ear[8] = [null, targetMode, null, null, null, [window.__capturedSubId]];
      }
      ear[24] = null;
      u.searchParams.set('ear', JSON.stringify(ear));
      window.__capturedEarUrl = u.toString();
      return u.toString();
    } catch (e) {
      return val;
    }
  }

  if (window.TrustedTypePolicy && TrustedTypePolicy.prototype.createScriptURL) {
    const origCreate = TrustedTypePolicy.prototype.createScriptURL;
    TrustedTypePolicy.prototype.createScriptURL = function(input, ...args) {
      return origCreate.call(this, rewriteUrl(input), ...args);
    };
  }
  const frameProto = HTMLIFrameElement.prototype;
  const srcDesc = Object.getOwnPropertyDescriptor(frameProto, 'src');
  if (srcDesc?.set) {
    Object.defineProperty(frameProto, 'src', {
      ...srcDesc,
      set(v) {
        if (typeof v === 'string') v = rewriteUrl(v);
        return srcDesc.set.call(this, v);
      }
    });
  }
  const origSetAttr = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function(name, val) {
    if (this instanceof HTMLIFrameElement && String(name).toLowerCase() === 'src') {
      val = rewriteUrl(String(val));
    }
    return origSetAttr.call(this, name, val);
  };
})();
"""


def get_totp(secret: str) -> str:
    if not secret:
        return ""
    secret = re.sub(r"[^A-Za-z2-7]", "", secret.strip()).upper()
    if not secret:
        return ""
    missing_padding = len(secret) % 8
    if missing_padding != 0:
        secret += "=" * (8 - missing_padding)
    key = base64.b32decode(secret, casefold=True)
    counter = int(time.time()) // 30
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[19] & 0xF
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


def get_profile_dir(acc_id: str, email: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", email or acc_id)
    path = os.path.join(PROFILES_DIR, f"profile_{safe_name}")
    os.makedirs(path, exist_ok=True)
    return path


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.8)
        return s.connect_ex(("127.0.0.1", port)) == 0


def parse_proxy_string(raw_proxy: str) -> dict[str, Any] | None:
    """
    Parse various proxy formats into {scheme, host, port, username, password}:
    - http://user:pass@host:port
    - socks5://user:pass@host:port
    - host:port:user:pass
    - user:pass@host:port
    - host:port
    """
    s = (raw_proxy or "").strip()
    if not s:
        return None

    scheme = "http"
    if "://" in s:
        scheme, s = s.split("://", 1)
        scheme = scheme.lower()

    if "@" in s:
        auth_part, host_part = s.rsplit("@", 1)
        user, pwd = (auth_part.split(":", 1) + [""])[:2]
        host, port_str = (host_part.split(":", 1) + ["8080"])[:2]
        return {
            "scheme": scheme,
            "host": host,
            "port": int(port_str),
            "username": user,
            "password": pwd,
        }

    parts = s.split(":")
    if len(parts) == 4:
        # host:port:user:pass
        return {
            "scheme": scheme,
            "host": parts[0],
            "port": int(parts[1]),
            "username": parts[2],
            "password": parts[3],
        }
    elif len(parts) == 2:
        return {
            "scheme": scheme,
            "host": parts[0],
            "port": int(parts[1]),
            "username": "",
            "password": "",
        }
    return None


_FLOPPY_CACHE: dict[str, str] = {}


def resolve_account_proxy(
    account: dict[str, Any], target_region: str, proxy_template: str
) -> str:
    """
    Resolve the proxy string for an account:
    1. Account-specific proxy overrides global template
    2. If raw string is a FloppyData API Key (32-char alphanumeric without :/@), auto-fetch subuser credentials
    3. Global proxy template replaces {country}/{COUNTRY} and {session}/{SESSION} automatically
    """
    raw = (account.get("proxy") or "").strip() or (proxy_template or "").strip()
    if not raw:
        return ""
    if ":" not in raw and "@" not in raw and len(raw) >= 24:
        if raw in _FLOPPY_CACHE:
            raw = _FLOPPY_CACHE[raw]
        else:
            try:
                import urllib.request

                req = urllib.request.Request(
                    "https://api.floppydata.net/v2/proxy/rotating/subusers",
                    headers={
                        "X-Api-Key": raw,
                        "User-Agent": "Mozilla/5.0",
                        "Connection": "close",
                    },
                )
                resp = json.loads(
                    urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
                )
                items = resp.get("items") or []
                if items:
                    u = items[0]["username"]
                    p = items[0]["password"]
                    raw = f"http://{u}-type-residential-country-{{COUNTRY}}-session-{{session}}-rotation-0:{p}@geo.g-w.info:10080"
                    _FLOPPY_CACHE[proxy_template.strip()] = raw
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                _LOGGER.debug("Proxy credential lookup failed", exc_info=True)
    cc_lower = (target_region or "TR").lower()
    cc_upper = cc_lower.upper()
    sess_seed = (
        re.sub(r"[^a-zA-Z0-9]", "", account.get("id", "acc"))
        + hashlib.md5(
            (account.get("email", "") or account.get("id", "")).encode("utf-8")
        ).hexdigest()[:6]
    )
    resolved = (
        raw.replace("{country}", cc_lower)
        .replace("{COUNTRY}", cc_upper)
        .replace("{session}", sess_seed)
        .replace("{SESSION}", sess_seed)
    )
    return resolved


_BLOCKED_PROXY_HOSTS = (
    b"mtalk.google.com",
    b"update.googleapis.com",
    b"safebrowsing.googleapis.com",
    b"google-ohttp-relay-safebrowsing",
    b"optimizationguide-pa.googleapis.com",
    b"gvt1.com",
    b"gvt2.com",
)


class LocalHttpProxyAuthBridge:
    """
    Lightweight local asyncio HTTP/HTTPS CONNECT proxy bridge that injects
    Proxy-Authorization Basic headers, blocks Chrome background telemetry bursts,
    verifies upstream CONNECT responses, and auto-rotates sticky residential sessions
    when a residential peer goes offline or drops connections.
    """

    def __init__(self, local_port: int, upstream: dict[str, Any]):
        self.local_port = local_port
        self.upstream = upstream
        self.server: asyncio.AbstractServer | None = None
        self.rotation_idx: int = 0
        self.fail_streak: int = 0

    def rotate_session(self):
        self.rotation_idx += 1
        self.fail_streak = 0

    def _current_username(self) -> str:
        base_u = self.upstream.get("username", "")
        if self.rotation_idx <= 0 or "-session-" not in base_u:
            return base_u
        return re.sub(
            r"(-session-[a-zA-Z0-9_]+)", rf"\1r{self.rotation_idx}", base_u, count=1
        )

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while not reader.at_eof():
                data = await reader.read(16384)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except OSError:
            _LOGGER.debug("Proxy stream cleanup failed", exc_info=True)
        finally:
            try:
                writer.close()
            except OSError:
                _LOGGER.debug("Proxy stream cleanup failed", exc_info=True)

    async def _handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ):
        up_writer = None
        try:
            first_line = await asyncio.wait_for(client_reader.readline(), timeout=15)
            if not first_line:
                client_writer.close()
                return

            fl_lower = first_line.lower()
            if any(bh in fl_lower for bh in _BLOCKED_PROXY_HOSTS):
                client_writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"
                )
                await client_writer.drain()
                client_writer.close()
                return

            headers = []
            while True:
                line = await asyncio.wait_for(client_reader.readline(), timeout=15)
                if not line or line in (b"\r\n", b"\n"):
                    break
                if not line.lower().startswith(b"proxy-authorization:"):
                    headers.append(line)

            is_connect = fl_lower.startswith(b"connect ")
            up_reader = None

            for attempt in range(3):
                try:
                    up_reader, up_writer = await asyncio.wait_for(
                        asyncio.open_connection(
                            self.upstream["host"], self.upstream["port"]
                        ),
                        timeout=12,
                    )
                    auth_str = f"{self._current_username()}:{self.upstream['password']}"
                    b64_auth = base64.b64encode(auth_str.encode("utf-8")).decode(
                        "ascii"
                    )
                    auth_header = f"Proxy-Authorization: Basic {b64_auth}\r\n".encode(
                        "ascii"
                    )

                    up_writer.write(first_line)
                    for h in headers:
                        up_writer.write(h)
                    up_writer.write(auth_header)
                    up_writer.write(b"\r\n")
                    await up_writer.drain()

                    if is_connect:
                        resp_hdr = await asyncio.wait_for(
                            up_reader.readuntil(b"\r\n\r\n"), timeout=12
                        )
                        status_line = resp_hdr.split(b"\r\n", 1)[0]
                        if b" 200 " not in status_line:
                            raise RuntimeError(
                                f"Upstream CONNECT non-200: {status_line!r}"
                            )
                        self.fail_streak = 0
                        client_writer.write(resp_hdr)
                        await client_writer.drain()
                    break
                except (
                    OSError,
                    TimeoutError,
                    asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError,
                    RuntimeError,
                ):
                    if up_writer:
                        try:
                            up_writer.close()
                        except OSError:
                            _LOGGER.debug("Proxy stream cleanup failed", exc_info=True)
                        up_writer = None
                    self.fail_streak += 1
                    if self.fail_streak >= 3 or attempt >= 1:
                        self.rotate_session()
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.5)

            if up_reader is None or up_writer is None:
                raise RuntimeError("Proxy connection did not provide both streams")
            await asyncio.gather(
                self._pipe(client_reader, up_writer),
                self._pipe(up_reader, client_writer),
            )
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            RuntimeError,
        ):
            try:
                client_writer.close()
            except OSError:
                _LOGGER.debug("Proxy stream cleanup failed", exc_info=True)
            if up_writer:
                try:
                    up_writer.close()
                except OSError:
                    _LOGGER.debug("Proxy stream cleanup failed", exc_info=True)

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", self.local_port
        )

    async def stop(self):
        if self.server:
            self.server.close()
            try:
                await self.server.wait_closed()
            except OSError:
                _LOGGER.debug("Proxy stream cleanup failed", exc_info=True)


class GoogleUltraAutomator:
    def __init__(
        self,
        account: dict[str, Any],
        card: dict[str, str],
        target_region: str = "TR",
        target_plan: str = "ultra_20tb_5x",
        auto_cancel: bool = True,
        proxy_template: str = "",
        port: int = 9320,
        on_log: Callable[[str], None] | None = None,
        on_state: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.account = account
        self.card = card
        self.target_region = (target_region or "TR").upper()
        self.target_plan = target_plan
        self.auto_cancel = auto_cancel
        self.proxy_template = proxy_template
        self.port = port
        self.on_log = on_log or (lambda msg: print(msg))
        self.on_state = on_state or (lambda st: None)
        self.stop_requested = False
        self.proc: asyncio.subprocess.Process | None = None
        self.proxy_bridge: LocalHttpProxyAuthBridge | None = None

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] [{self.account['id']}] {msg}"
        self.on_log(line)

    def update_state(
        self,
        status: str | None = None,
        current_step: int | None = None,
        step_message: str | None = None,
        expiry_date: str | None = None,
        last_screenshot: str | None = None,
        ip_info: str | None = None,
    ):
        patch: dict[str, Any] = {}
        if status is not None:
            self.account["status"] = status
            patch["status"] = status
        if current_step is not None:
            self.account["current_step"] = current_step
            patch["current_step"] = current_step
        if step_message is not None:
            self.account["step_message"] = step_message
            patch["step_message"] = step_message
        if expiry_date is not None:
            self.account["expiry_date"] = expiry_date
            patch["expiry_date"] = expiry_date
        if last_screenshot is not None:
            self.account["last_screenshot"] = last_screenshot
            patch["last_screenshot"] = last_screenshot
        if ip_info is not None:
            self.account["ip_info"] = ip_info
            patch["ip_info"] = ip_info
        self.on_state(patch)

    async def save_screenshot(self, page, step_tag: str) -> str:
        fname = f"{self.account['id']}_{step_tag}_{int(time.time())}.png"
        fpath = os.path.join(SCREENSHOTS_DIR, fname)
        try:
            await page.screenshot(path=fpath)
            self.update_state(last_screenshot=fname)
        except (PlaywrightError, OSError) as e:
            self.log(f"Screenshot save notice: {e}")
        return fname

    def check_stop(self):
        if self.stop_requested:
            raise RuntimeError("Task stopped by user")

    async def safe_goto(self, page, url: str, timeout: int = 40000):
        """Navigate with automatic retry and residential proxy session rotation on transient network errors."""
        for attempt in range(4):
            self.check_stop()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                return
            except PlaywrightError as e:
                if attempt == 3:
                    raise
                err_short = (str(e).splitlines() or [type(e).__name__])[0][:80]
                if attempt < 3:
                    if self.proxy_bridge:
                        self.proxy_bridge.rotate_session()
                    self.log(
                        f"[WARN] Residential proxy network interruption ({err_short}); switching to another node in the same country and retrying ({attempt + 1}/3)..."
                    )
                    await asyncio.sleep(2.5)

    async def ensure_totp_fresh(self) -> str:
        secret = self.account.get("totp_secret", "")
        if not secret:
            return ""
        rem = 30 - (int(time.time()) % 30)
        if rem <= 3:
            self.log(f"2FA code is about to refresh; waiting {rem + 1} seconds...")
            await asyncio.sleep(rem + 1)
        return get_totp(secret)

    async def pass_reauth_on_page(self, page_obj):
        """Automatically handle Google email + password + 2FA TOTP re-authentication prompts."""
        email = self.account.get("email", "")
        password = self.account.get("password", "")
        for _ in range(16):
            self.check_stop()
            if page_obj.is_closed():
                return
            try:
                body = await page_obj.inner_text("body", timeout=3000)
            except PlaywrightError:
                await asyncio.sleep(1.5)
                continue

            if (
                "gds.google.com" in page_obj.url
                or "recoveryoptions" in page_obj.url
                or "Make sure you can always sign in" in body
            ):
                self.log(
                    "Detected the gds.google.com recovery information page; clicking Cancel/Skip to continue sign-in..."
                )
                cancel_btn = page_obj.locator(
                    'button:has-text("Cancel"), button:has-text("Skip"), [role="button"]:has-text("Cancel"), [role="button"]:has-text("Skip"), text="Cancel"'
                ).first
                if await cancel_btn.count() > 0:
                    try:
                        await cancel_btn.click(timeout=3000)
                    except PlaywrightError:
                        _LOGGER.debug("Optional browser action failed", exc_info=True)
                await page_obj.evaluate(
                    """() => {
                    const els = Array.from(document.querySelectorAll('button, a, [role="button"], span, div'));
                    for (const el of els) {
                        const t = (el.innerText || '').trim();
                        if (['Cancel', 'Skip', 'Not now'].includes(t)) {
                            el.click();
                        }
                    }
                }"""
                )
                await asyncio.sleep(4.5)
                continue

            if "confirmidentifier" in page_obj.url:
                self.log("Detected the confirmidentifier page; clicking Next...")
                nxt_btn = page_obj.locator(
                    '#identifierNext button, button:has-text("Next"), #identifierNext'
                ).first
                if await nxt_btn.count() > 0:
                    try:
                        await nxt_btn.click(timeout=3000)
                    except PlaywrightError:
                        try:
                            await nxt_btn.click(force=True, timeout=3000)
                        except PlaywrightError:
                            _LOGGER.debug(
                                "Optional browser action failed", exc_info=True
                            )
                await page_obj.evaluate(
                    """() => {
                    const targets = Array.from(document.querySelectorAll('button, #identifierNext, [role="button"]'));
                    for (const el of targets) {
                        const t = (el.innerText || '').trim();
                        if (t === 'Next' || t === 'Continue' || el.id === 'identifierNext') {
                            el.click();
                        }
                    }
                }"""
                )
                await asyncio.sleep(4)
                continue

            email_inp = page_obj.locator('input#identifierId, input[name="identifier"]')
            if (
                email
                and await email_inp.count() > 0
                and await email_inp.first.is_visible()
            ):
                pwd_vis = page_obj.locator(
                    'input[type="password"], input[name="Passwd"]'
                )
                if await pwd_vis.count() == 0 or not await pwd_vis.first.is_visible():
                    self.log(
                        f"Detected the email field; entering email and clicking Next: {email}"
                    )
                    await email_inp.first.fill(email)
                    await email_inp.first.press("Enter")
                    await asyncio.sleep(4.5)
                    continue

            if "Verify it's you" in body:
                btn = page_obj.locator(
                    'button:has-text("Verify it\'s you"), div[role="button"]:has-text("Verify it\'s you")'
                ).first
                if await btn.count() > 0 and await btn.is_visible():
                    self.log("Detected identity verification button; clicking it...")
                    await btn.click()
                    await asyncio.sleep(4)
                    continue

            pwd = page_obj.locator('input[type="password"], input[name="Passwd"]')
            if await pwd.count() > 0 and await pwd.first.is_visible():
                self.log("Entering password for authentication...")
                try:
                    await pwd.first.fill(password)
                    next_btn = page_obj.locator(
                        '#passwordNext, button:has-text("Next")'
                    ).first
                    if await next_btn.count() > 0 and await next_btn.is_visible():
                        await next_btn.click()
                    else:
                        await pwd.first.press("Enter")
                except PlaywrightError:
                    return
                await asyncio.sleep(4.5)
                continue

            if (
                "challenge/recaptcha" in page_obj.url
                or "Prove you are not a robot" in body
                or "not a robot" in body
            ):
                for fr in page_obj.frames:
                    if "recaptcha" in (fr.url or "") and "anchor" in (fr.url or ""):
                        try:
                            cb = fr.locator("#recaptcha-anchor")
                            if await cb.count() > 0:
                                self.log(
                                    "Detected sign-in verification checkbox (challenge/recaptcha); selecting it..."
                                )
                                await cb.click()
                                await asyncio.sleep(4.5)
                                break
                        except PlaywrightError:
                            _LOGGER.debug(
                                "Optional browser action failed", exc_info=True
                            )
                continue

            # Check if an open dialog modal (VfPpkd-Sx9Kwc-OWXEXe-FNFY6c) is shown on top of the page
            open_dialog_btn = page_obj.locator(
                'div.VfPpkd-Sx9Kwc-OWXEXe-FNFY6c button, div.VfPpkd-Sx9Kwc-OWXEXe-FNFY6c [role="button"], div[role="dialog"] button, div[role="dialog"] [role="button"]'
            )
            if await open_dialog_btn.count() > 0:
                for idx in range(await open_dialog_btn.count()):
                    b = open_dialog_btn.nth(idx)
                    if await b.is_visible():
                        btxt = (await b.inner_text()).strip()
                        if any(
                            k in btxt
                            for k in (
                                "Not now",
                                "Skip",
                                "Cancel",
                                "No thanks",
                                "Google Authenticator",
                            )
                        ):
                            self.log(f"Detected dialog option [{btxt}], clicking...")
                            await b.click(force=True)
                            await asyncio.sleep(3.5)
                            break

            if "Google Authenticator" in body:
                totp_vis = page_obj.locator(
                    'input[type="tel"], input#totpPin, input[name="totpPin"]'
                )
                if await totp_vis.count() == 0 or not await totp_vis.first.is_visible():
                    opt = page_obj.locator(
                        "text=/Google Authenticator/i"
                    ).first
                    if await opt.count() > 0 and await opt.is_visible():
                        self.log("Selecting Google Authenticator...")
                        await opt.click(force=True)
                        await asyncio.sleep(3.5)
                        continue
            elif (
                "challenge/dp" in page_obj.url
                or "challenge/az" in page_obj.url
                or "challenge/pk" in page_obj.url
                or "Scan a QR code" in body
            ) and "challenge/totp" not in page_obj.url:
                totp_vis = page_obj.locator(
                    'input[type="tel"], input#totpPin, input[name="totpPin"]'
                )
                if await totp_vis.count() == 0 or not await totp_vis.first.is_visible():
                    try_other = page_obj.locator(
                        "text=/Try another way/i"
                    ).first
                    if await try_other.count() > 0 and await try_other.is_visible():
                        self.log(
                            "Detected another verification method (QR code, push, or key); switching to a 2FA code..."
                        )
                        try:
                            await try_other.click(timeout=4000)
                        except PlaywrightError:
                            _LOGGER.debug(
                                "Optional browser action failed", exc_info=True
                            )
                        await asyncio.sleep(3.5)
                        continue

            if "Confirm your recovery email" in body:
                rec_email = (self.account.get("recovery_email") or "").strip()
                kpe_inp = page_obj.locator(
                    'input#knowledge-preregistered-email-response:visible, input[name="knowledgePreregisteredEmailResponse"]:visible, input[type="email"]:visible'
                )
                if rec_email and await kpe_inp.count() > 0:
                    self.log(f"Entering recovery email for verification: {rec_email}")
                    await kpe_inp.first.fill(rec_email)
                    await kpe_inp.first.press("Enter")
                    await asyncio.sleep(4.5)
                    continue
                opt = page_obj.locator(
                    "text=/Confirm your recovery email/i"
                ).first
                if await opt.count() > 0 and await opt.is_visible():
                    self.log("Selecting recovery email verification...")
                    await opt.click(force=True)
                    await asyncio.sleep(3.5)
                    continue

            if (
                "Add 2-Step Verification" in body
                or "We couldn’t verify it’s you" in body
                or "We couldn't verify it's you" in body
                or "signin/rejected" in page_obj.url
            ):
                self.log(
                    "[INFO] Detected new-device security page (Add 2-Step Verification / signin/rejected); skipping separate verification page..."
                )
                return

            totp = page_obj.locator(
                'input[type="tel"], input#totpPin, input[name="totpPin"]'
            )
            if await totp.count() > 0 and await totp.first.is_visible():
                code = await self.ensure_totp_fresh()
                if code:
                    self.log(f"Entering 2FA code: {code}")
                    try:
                        await totp.first.fill(code)
                        next_btn = page_obj.locator(
                            '#totpNext, button:has-text("Next")'
                        ).first
                        if await next_btn.count() > 0 and await next_btn.is_visible():
                            await next_btn.click()
                        else:
                            await totp.first.press("Enter")
                    except PlaywrightError:
                        return
                    await asyncio.sleep(5)
                    continue

            if (
                "accounts.google.com" in page_obj.url
                or "gds.google.com" in page_obj.url
                or page_obj.url == "about:blank"
            ):
                await asyncio.sleep(2)
                continue

            break

    async def step1_login(self, page):
        self.update_state(
            status="running",
            current_step=1,
            step_message="Step 1/5: Signing in to Google and completing verification (recovery email / 2FA)...",
        )
        self.log(
            "=== Step 1/5: Checking Google sign-in and completing recovery email / 2FA verification ==="
        )
        email = self.account["email"]
        _password = self.account["password"]

        await self.safe_goto(
            page,
            "https://accounts.google.com/ServiceLogin?hl=en-US&continue=https%3A%2F%2Fone.google.com%2Fplans%3Fhl%3Den-US",
            timeout=40000,
        )
        await asyncio.sleep(3.5)
        await self.save_screenshot(page, "step1_init")

        for login_round in range(3):
            if (
                "one.google.com" in page.url
                and "accounts.google.com" not in page.url
                and "gds.google.com" not in page.url
            ):
                break
            try:
                body_txt = await page.inner_text("body", timeout=3000)
            except PlaywrightError:
                body_txt = ""

            if (
                "gds.google.com" in page.url
                or "recoveryoptions" in page.url
                or "homeaddress" in page.url
                or "Make sure you can always sign in" in body_txt
                or "Set a home address" in body_txt
            ):
                await page.evaluate(
                    """() => {
                    const els = Array.from(document.querySelectorAll('button, a, [role="button"], span'));
                    for (const el of els) {
                        const t = (el.innerText || '').trim();
                        if (['Cancel', 'Skip', 'Not now'].includes(t)) el.click();
                    }
                }"""
                )
                await asyncio.sleep(4.0)
                if (
                    "one.google.com" in page.url
                    and "accounts.google.com" not in page.url
                ):
                    break

            # Account chooser check
            email_lower = email.lower()
            email_clean = email_lower.replace(".", "")
            chooser_items = page.locator("[data-identifier]")
            clicked_chooser = False
            for i in range(await chooser_items.count()):
                ident = (
                    await chooser_items.nth(i).get_attribute("data-identifier") or ""
                ).lower()
                if ident == email_lower or ident.replace(".", "") == email_clean:
                    self.log(f"Selecting account in account chooser: {ident}")
                    await chooser_items.nth(i).click()
                    clicked_chooser = True
                    await asyncio.sleep(3.5)
                    break

            if not clicked_chooser:
                use_other = page.locator("text=/Use another account/i")
                email_loc = page.locator('input[type="email"], input#identifierId')
                if (
                    await email_loc.count() == 0
                    or not await email_loc.first.is_visible()
                ) and await use_other.count() > 0:
                    await use_other.first.click()
                    await asyncio.sleep(2.5)

                email_loc = page.locator('input[type="email"], input#identifierId')
                if (
                    await email_loc.count() > 0
                    and await email_loc.first.is_visible()
                    and "challenge/kpe" not in page.url
                ):
                    self.log(f"Entering email: {email}")
                    await email_loc.first.fill(email)
                    next_btn = page.locator(
                        '#identifierNext, button:has-text("Next")'
                    ).first
                    if await next_btn.count() > 0 and await next_btn.is_visible():
                        await next_btn.click()
                    else:
                        await email_loc.first.press("Enter")
                    await asyncio.sleep(4.5)

            # Password + Recovery Email + 2FA
            await self.pass_reauth_on_page(page)

            # Dismiss post-login prompts
            for _ in range(4):
                if "one.google.com" in page.url and "gds.google.com" not in page.url:
                    break
                btn = page.locator(
                    'button:has-text("Not now"), button:has-text("Skip"), button:has-text("Cancel"), button:has-text("I agree"), button:has-text("Continue")'
                )
                if await btn.count() > 0 and await btn.first.is_visible():
                    await btn.first.click(force=True)
                    await asyncio.sleep(3.5)
                else:
                    break

            if "signin/identifier" in page.url or "ServiceLogin" in page.url:
                if self.proxy_bridge:
                    self.proxy_bridge.rotate_session()
                self.log("[WARN] Sign-in page is slow; retrying submission...")
                await self.safe_goto(
                    page,
                    "https://accounts.google.com/ServiceLogin?hl=en-US&continue=https%3A%2F%2Fone.google.com%2Fplans%3Fhl%3Den-US",
                    timeout=40000,
                )
                await asyncio.sleep(3.5)

        await self.save_screenshot(page, "step1_done")
        self.log(f"[OK] Step 1 complete; current page: {page.url[:80]}")

    async def get_current_sub_id(self, page) -> str | None:
        for url in (
            "https://one.google.com/settings?expand=upgrade&hl=en-US",
            "https://one.google.com/plans?hl=en-US",
            "https://one.google.com/ai-nye?g1_landing_page=0&hl=en-US",
        ):
            await self.safe_goto(page, url, timeout=40000)
            for _ in range(6):
                await asyncio.sleep(1.8)
                html = await page.content()
                ids = list(set(re.findall(r"\"([a-z]{24})\.AO-", html)))
                if not ids:
                    ids = list(set(re.findall(r"([a-z]{24})\.AO-J1O", html)))
                if not ids:
                    # Match 24-char lowercase subscription token inside AF_initDataCallback arrays
                    for blk in re.findall(
                        r"AF_initDataCallback\(\{.*?\}\);", html, flags=re.DOTALL
                    ):
                        if "subscriptions.red" in blk or "g1." in blk:
                            cand = re.findall(r"\"([a-z]{24})\"", blk)
                            if cand:
                                ids = cand
                                break
                if ids:
                    sub_id = ids[0]
                    self.log(f"Current 24-digit subscription ID: {sub_id}")
                    return sub_id
        self.log(
            "[WARN] Could not extract a 24-digit subscription ID from the static HTML; the Ngbm interceptor will retain the original ID"
        )
        return None

    async def inspect_play_subscriptions(self, page) -> str:
        await self.safe_goto(
            page,
            "https://play.google.com/store/account/subscriptions?hl=en-US",
            timeout=40000,
        )
        await asyncio.sleep(3)
        if "accounts.google.com" in page.url or "gds.google.com" in page.url:
            await self.pass_reauth_on_page(page)
            await asyncio.sleep(3)
        txt = await page.inner_text("body")
        clean = " | ".join([l.strip() for l in txt.splitlines() if l.strip()])
        return clean

    async def ensure_subscription_active(self, page):
        """If the account has a cancelled subscription, click Resubscribe so Mode 5 stacking works."""
        await self.safe_goto(
            page,
            "https://play.google.com/store/account/subscriptions?hl=en-US",
            timeout=40000,
        )
        await asyncio.sleep(3.5)
        if "accounts.google.com" in page.url or "gds.google.com" in page.url:
            await self.pass_reauth_on_page(page)
            await asyncio.sleep(3.0)
        try:
            body = await page.inner_text("body", timeout=3000)
        except PlaywrightError:
            body = ""
        if "resubscribe" in body.lower():
            self.log(
                "Detected a cancelled subscription; clicking Resubscribe to restore it..."
            )
            rbtn = page.get_by_text("Resubscribe", exact=True).first
            if await rbtn.count() > 0:
                await rbtn.click(force=True)
                await asyncio.sleep(3.5)
                cbtn = page.get_by_text("Resubscribe", exact=True).last
                if await cbtn.count() > 0 and await cbtn.is_visible():
                    await cbtn.click(force=True)
                    await asyncio.sleep(5.0)
                ok_btn = page.get_by_text("OK", exact=True).first
                if await ok_btn.count() > 0 and await ok_btn.is_visible():
                    await ok_btn.click(force=True)
                    await asyncio.sleep(2.0)

    async def _poll_eacquire_modal(self, page, max_loops: int = 55, step_tag: str = ""):
        last_confirm_click_ts = 0.0
        loops_left = max_loops
        inline_card_saved = False
        while loops_left > 0:
            loops_left -= 1
            self.check_stop()
            if "accounts.google.com" in page.url or "gds.google.com" in page.url:
                self.log(
                    f"[{step_tag or 'modal'}] Detected redirect to sign-in verification ({page.url[:60]}); completing authentication..."
                )
                await self.pass_reauth_on_page(page)
                return None, ""
            frames_to_check = []
            for p in page.context.pages:
                frames_to_check.extend(p.frames)

            for fr in frames_to_check:
                fr_url = fr.url or ""
                if "eacquire" in fr_url:
                    try:
                        state_info = await fr.evaluate(
                            """() => {
                            if (!document.body) return {ready: false, text: ''};
                            const clone = document.body.cloneNode(true);
                            clone.querySelectorAll('script, style, noscript').forEach(el => el.remove());
                            const text = (clone.innerText || '').trim();
                            const needsCard = ['Add a payment method', 'First add a payment method', 'Add credit or debit card', 'Add card'].some(label => text.includes(label));
                            const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                            const hasSubBtn = !needsCard && btns.some(b => {
                                const bt = (b.innerText || '').trim().toLowerCase();
                                const r = b.getBoundingClientRect();
                                return bt === 'subscribe' && r.width > 10 && r.height > 10;
                            });
                            const hasErr = ['something went wrong', 'unable to change', 'OR-', 'BX-'].some(label => text.toLowerCase().includes(label.toLowerCase()));
                            return {ready: hasSubBtn || hasErr, text: text};
                        }"""
                        )
                        if state_info and state_info.get("ready"):
                            raw_t = state_info.get("text", "")
                            clean_txt = " | ".join(
                                [l.strip() for l in raw_t.splitlines() if l.strip()]
                            )
                            return fr, clean_txt
                        elif state_info and not inline_card_saved:
                            raw_t = state_info.get("text", "")
                            if any(
                                k in raw_t
                                for k in (
                                    "Add a payment method",
                                    "First add a payment method",
                                    "Add credit or debit card",
                                    "Add card",
                                    "Add a payment method",
                                    "Add credit or debit card",
                                    "Add card",
                                )
                            ):
                                add_btn = fr.locator(
                                    "text=/Add credit or debit card|Add card/i"
                                ).last
                                if (
                                    await add_btn.count() > 0
                                    and await add_btn.is_visible()
                                ):
                                    self.log(
                                        f"[{step_tag or 'modal'}] Dialog requires a payment method; adding a card inline (ZIP 97201)..."
                                    )
                                    try:
                                        await add_btn.click(force=True)
                                    except PlaywrightError:
                                        _LOGGER.debug(
                                            "Optional browser action failed",
                                            exc_info=True,
                                        )
                                    await fr.evaluate(
                                        """() => {
                                        const els = Array.from(document.querySelectorAll('button, [role="button"], div, span'));
                                        for (let i = els.length - 1; i >= 0; i--) {
                                            const t = (els[i].innerText || '').trim();
                                            if (t === 'Add card' || t === 'Add credit or debit card') {
                                                els[i].click();
                                                break;
                                            }
                                        }
                                    }"""
                                    )
                                    for _ in range(20):
                                        await asyncio.sleep(1.0)
                                        all_frames = list(page.frames)
                                        for tf in all_frames:
                                            try:
                                                vis = tf.locator("input:visible")
                                                cnt = await vis.count()
                                                if cnt >= 4:
                                                    cnum = (
                                                        self.card.get("number") or ""
                                                    ).replace(" ", "")
                                                    expm = (
                                                        self.card.get("exp_month") or ""
                                                    ).zfill(2)
                                                    expy = (
                                                        self.card.get("exp_year") or ""
                                                    )[-2:]
                                                    cvc_v = (
                                                        self.card.get("cvc") or ""
                                                    ).strip()
                                                    holder_v = (
                                                        self.card.get("holder")
                                                        or "CARD HOLDER"
                                                    ).strip()
                                                    zip_v = (
                                                        self.card.get("postal_code")
                                                        or "97201"
                                                    ).strip()
                                                    await vis.nth(0).click(force=True)
                                                    await asyncio.sleep(0.2)
                                                    await vis.nth(0).press_sequentially(
                                                        cnum, delay=35
                                                    )
                                                    await asyncio.sleep(0.3)
                                                    await vis.nth(1).click(force=True)
                                                    await asyncio.sleep(0.2)
                                                    await vis.nth(1).press_sequentially(
                                                        f"{expm}{expy}", delay=35
                                                    )
                                                    await asyncio.sleep(0.3)
                                                    await vis.nth(2).click(force=True)
                                                    await asyncio.sleep(0.2)
                                                    await vis.nth(2).press_sequentially(
                                                        cvc_v, delay=35
                                                    )
                                                    await asyncio.sleep(0.3)
                                                    try:
                                                        cur_name = (
                                                            await vis.nth(
                                                                3
                                                            ).input_value(timeout=1000)
                                                        ).strip()
                                                        if not cur_name:
                                                            await vis.nth(3).fill(
                                                                holder_v
                                                            )
                                                            await asyncio.sleep(0.2)
                                                    except PlaywrightError:
                                                        _LOGGER.debug(
                                                            "Optional browser action failed",
                                                            exc_info=True,
                                                        )
                                                    if cnt >= 5:
                                                        await vis.nth(4).click(
                                                            force=True
                                                        )
                                                        await asyncio.sleep(0.2)
                                                        await vis.nth(4).fill("")
                                                        await vis.nth(
                                                            4
                                                        ).press_sequentially(
                                                            zip_v, delay=35
                                                        )
                                                        await asyncio.sleep(0.3)
                                                    sbtn = tf.locator(
                                                        'button:has-text("Save card"), button:has-text("Save"), [role="button"]:has-text("Save")'
                                                    ).first
                                                    if await sbtn.count() > 0:
                                                        await sbtn.click(force=True)
                                                    else:
                                                        await tf.evaluate(
                                                            """() => {
                                                            const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                                                            const b = btns.find(e => (e.innerText || '').includes('Save'));
                                                            if (b) b.click();
                                                        }"""
                                                        )
                                                    self.log(
                                                        f"[{step_tag or 'modal'}] [OK] Clicked Save card; waiting for Google Play confirmation..."
                                                    )
                                                    for _w in range(22):
                                                        await asyncio.sleep(1.0)
                                                        still_im = any(
                                                            "instrumentmanager"
                                                            in (f.url or "")
                                                            for f in page.frames
                                                        )
                                                        if not still_im:
                                                            self.log(
                                                                f"[{step_tag or 'modal'}] [OK] Inline card saved!"
                                                            )
                                                            break
                                                        if (
                                                            _w == 7
                                                            and await sbtn.count() > 0
                                                        ):
                                                            try:
                                                                await sbtn.click(
                                                                    force=True
                                                                )
                                                            except PlaywrightError:
                                                                _LOGGER.debug(
                                                                    "Optional browser action failed",
                                                                    exc_info=True,
                                                                )
                                                    await asyncio.sleep(2.0)
                                                    inline_card_saved = True
                                                    loops_left = 15
                                                    break
                                            except PlaywrightError:
                                                _LOGGER.debug(
                                                    "Optional browser action failed",
                                                    exc_info=True,
                                                )
                                        if inline_card_saved:
                                            break
                    except PlaywrightError:
                        _LOGGER.debug("Optional browser action failed", exc_info=True)

            # Also check for an intermediate confirmation dialog throughout the wait.
            has_eacquire_frame = any(
                "eacquire" in (f.url or "") for f in frames_to_check
            )
            if not has_eacquire_frame and (time.time() - last_confirm_click_ts) >= 2.5:
                try:
                    confirm_btn = await page.evaluate(
                        """() => {
                        const targets = ['Continue to subscribe', 'Yes, continue', 'Switch to this plan', 'Resubscribe', 'Go to checkout', 'Get Google AI Ultra', 'I agree', 'Agree', 'Continue'];
                        const isTrulyOpenAndTop = (el) => {
                            const selfCs = window.getComputedStyle(el);
                            if (selfCs.visibility === 'hidden' || selfCs.display === 'none' || selfCs.pointerEvents === 'none') return false;
                            let cur = el;
                            while (cur && cur !== document.body && cur !== document.documentElement) {
                                if (cur.getAttribute && cur.getAttribute('aria-hidden') === 'true') return false;
                                const cs = window.getComputedStyle(cur);
                                if (cs.display === 'none' || parseFloat(cs.opacity) < 0.85) return false;
                                cur = cur.parentElement;
                            }
                            const r = el.getBoundingClientRect();
                            const cx = r.left + r.width / 2;
                            const cy = r.top + r.height / 2;
                            if (cx <= 0 || cy <= 0 || cx >= window.innerWidth || cy >= window.innerHeight) return false;
                            const topEl = document.elementFromPoint(cx, cy);
                            if (!topEl) return false;
                            return el.contains(topEl) || topEl.contains(el);
                        };
                        const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                        for (const b of btns) {
                            const r = b.getBoundingClientRect();
                            if (r.width <= 15 || r.height <= 15) continue;
                            if (!isTrulyOpenAndTop(b)) continue;
                            const t = (b.innerText || '').trim();
                            if (targets.some(lbl => t === lbl || t.includes(lbl))) {
                                b.click();
                                return {text: t, x: r.left + r.width / 2, y: r.top + r.height / 2};
                            }
                        }
                        return null;
                    }"""
                    )
                    if isinstance(confirm_btn, dict) and isinstance(
                        confirm_btn.get("text"), str
                    ):
                        last_confirm_click_ts = time.time()
                        self.log(
                            f"[{step_tag or 'modal'}] Clicked intermediate confirmation button: {confirm_btn['text']}"
                        )
                        loops_left = max(loops_left, 60)
                except PlaywrightError:
                    _LOGGER.debug("Optional browser action failed", exc_info=True)

            await asyncio.sleep(1.0)
        if inline_card_saved:
            return None, "INLINE_CARD_SAVED_RETRY"
        return None, ""

    async def open_and_click_subscribe(
        self,
        page,
        cfg: dict[str, Any],
        step_tag: str,
        expect_keywords: list | None = None,
    ) -> bool:
        self.last_subscribe_error = ""
        target_fr = None
        clean_txt = ""

        # Primary trigger: one.google.com/plans?hl=en-US clicking the LAST action button (proven 100% reliable across L1, L2, L3)
        for attempt_url in ("https://one.google.com/plans?hl=en-US",) * 5:
            if target_fr:
                break
            self.log(
                f"[{step_tag}] Opening plan page to trigger Ngbm checkout: {attempt_url}"
            )
            await self.safe_goto(page, attempt_url, timeout=40000)
            await asyncio.sleep(3.5)
            if "accounts.google.com" in page.url or "gds.google.com" in page.url:
                self.log(
                    f"[{step_tag}] Page redirected to sign-in verification; completing authentication..."
                )
                await self.pass_reauth_on_page(page)
                await self.safe_goto(page, attempt_url, timeout=40000)
                await asyncio.sleep(3.5)

            await page.evaluate(HOOK_JS)
            await page.evaluate(f"window.__setDirectTest({json.dumps(cfg)})")

            fb_btn = None
            for _ in range(10):
                await asyncio.sleep(1.2)
                await page.evaluate(HOOK_JS)
                await page.evaluate(f"window.__setDirectTest({json.dumps(cfg)})")
                fb_btn = await page.evaluate(
                    """() => {
                    const els = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                    const btns = els.filter(e => {
                        const t = (e.innerText || '').trim();
                        const r = e.getBoundingClientRect();
                        return r.width > 15 && r.height > 15 && r.left >= 240 && (
                            t.includes('Get started') ||
                            t.includes('Get offer') ||
                            t.includes('Get discount') ||
                            t.includes('Switch to this plan') ||
                            t === 'Upgrade' ||
                            t.includes('Get Google AI Ultra')
                        );
                    });
                    if (btns.length > 0) {
                        const btn = btns[btns.length - 1];
                        btn.scrollIntoView({block: 'center'});
                        const r = btn.getBoundingClientRect();
                        btn.click();
                        return {x: r.left + r.width / 2, y: r.top + r.height / 2, text: (btn.innerText || '').trim()};
                    }
                    return null;
                }"""
                )
                if fb_btn:
                    self.log(
                        f"[{step_tag}] Clicked [{fb_btn.get('text', '')}] to trigger checkout; waiting for Google Play..."
                    )
                    break

            if fb_btn:
                target_fr, clean_txt = await self._poll_eacquire_modal(
                    page, max_loops=60, step_tag=step_tag
                )
                if clean_txt == "INLINE_CARD_SAVED_RETRY":
                    self.log(
                        f"[{step_tag}] Card saved inline; reopening plan page to trigger the zero-cost checkout..."
                    )
                    continue

        await self.save_screenshot(page, f"{step_tag}_modal")

        if not target_fr:
            self.log(
                f"[WARN] [{step_tag}] Google Play checkout dialog not detected (eacquire/multiline)"
            )
            return False

        self.log(f"[{step_tag}] Checkout dialog text: {clean_txt[:350]}")
        if (
            "Error" in clean_txt
            or "Something went wrong" in clean_txt
            or "unable to change" in clean_txt
            or "Something went wrong" in clean_txt
        ):
            self.log(f"[WARN] [{step_tag}] Dialog returned an error: {clean_txt[:200]}")
            return False

        # Ensure the configured card (last4) is selected in the eacquire modal if multiple cards exist
        card_num_clean = re.sub(r"\s+", "", self.card.get("number", ""))
        target_last4 = card_num_clean[-4:] if len(card_num_clean) >= 4 else ""
        if (
            target_last4
            and target_last4 not in clean_txt
            and (
                "Mastercard-" in clean_txt
                or "Visa-" in clean_txt
                or "PaysafeCard" in clean_txt
            )
        ):
            self.log(
                f"[{step_tag}] Current default card does not end in {target_last4}; switching to card ending in {target_last4}..."
            )
            try:
                pm_loc = target_fr.locator(
                    "text=/Mastercard-|Visa-|PaysafeCard/i"
                ).first
                if await pm_loc.count() > 0:
                    await pm_loc.click(timeout=5000)
                    await asyncio.sleep(2.5)
                    await self.save_screenshot(page, f"{step_tag}_pm_list")
                    card_loc = target_fr.locator(f"text=/{target_last4}/").first
                    if await card_loc.count() > 0:
                        await card_loc.click(timeout=5000)
                        self.log(
                            f"[OK] [{step_tag}] Selected card ending in {target_last4} in checkout!"
                        )
                        await asyncio.sleep(3.0)
                        refreshed_fr, refreshed_txt = await self._poll_eacquire_modal(
                            page, max_loops=10
                        )
                        if refreshed_fr:
                            target_fr, clean_txt = refreshed_fr, refreshed_txt
                            self.log(
                                f"[{step_tag}] Checkout dialog text after switching cards: {clean_txt[:350]}"
                            )
                        await self.save_screenshot(
                            page, f"{step_tag}_modal_switched_{target_last4}"
                        )
                    else:
                        self.log(
                            f"[WARN] [{step_tag}] Payment methods did not include card ending in {target_last4}."
                        )
            except PlaywrightError as pm_err:
                self.log(f"[{step_tag}] Payment card switch notice: {pm_err}")

        # Require an explicit zero amount due now; a future renewal price is expected.
        if "ultra" not in step_tag.lower() and not _checkout_due_now_is_zero(clean_txt):
            self.last_subscribe_error = f"Zero-cost protection stopped checkout: could not verify a zero amount due now ({clean_txt[:180]})"
            self.log(f"[ERROR] [{step_tag}] {self.last_subscribe_error}")
            return False

        if expect_keywords:
            for kw in expect_keywords:
                if kw not in clean_txt:
                    self.log(
                        f"[WARN] [{step_tag}] Dialog did not include expected keyword '{kw}'; current text: {clean_txt[:200]}"
                    )

        all_btns = target_fr.locator('button, [role="button"]')
        bcnt = await all_btns.count()
        clicked = False
        for i in range(bcnt):
            b = all_btns.nth(i)
            try:
                btxt = (await b.inner_text()).strip()
                if btxt == "Subscribe":
                    self.log(f"--> Clicking [{step_tag}] [{btxt}] button...")
                    try:
                        await b.scroll_into_view_if_needed(timeout=3000)
                        await b.click(timeout=5000)
                        clicked = True
                    except PlaywrightError:
                        await b.click(force=True, timeout=5000)
                        clicked = True
                    break
            except PlaywrightError as click_err:
                self.log(
                    f"[{step_tag}] Normal click notice ({click_err}); switching to a native JS click..."
                )

        if not clicked:
            js_clicked = await target_fr.evaluate(
                """() => {
                const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                const sub = btns.find(b => {
                    const bt = (b.innerText || '').trim();
                    return bt === 'Subscribe';
                });
                if (sub) {
                    sub.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                    sub.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
                    sub.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    sub.click();
                    return true;
                }
                return false;
            }"""
            )
            if js_clicked:
                self.log(
                    f"--> Triggered [{step_tag}] Subscribe button via a native JS event!"
                )
                clicked = True

        if not clicked:
            self.log(f"[WARN] [{step_tag}] Subscribe button not found")
            return False

        # Monitor for CVC prompt, Google password re-auth, or post-click error/completion
        cvc_code = (self.card.get("cvc") or "").strip()
        acct_pwd = self.account.get("password", "")
        post_click_err = None
        reauth_spinning = False
        for wait_i in range(35):
            await asyncio.sleep(2)
            if len(page.context.pages) > 1:
                await self.pass_reauth_on_page(page.context.pages[-1])

            eacquire_present = False
            reauth_spinning = False
            ordered_frames = [f for f in page.frames if f != page.main_frame] + [
                page.main_frame
            ]
            for f2 in ordered_frames:
                f2_url = f2.url or ""
                if "eacquire" in f2_url:
                    eacquire_present = True
                try:
                    f2_txt = (await f2.inner_text("body", timeout=1500)).strip()
                except PlaywrightError:
                    f2_txt = ""

                if "Verify it's you" in f2_txt:
                    reauth_spinning = True

                if (
                    "eacquire" in f2_url
                    or "instrument" in f2_url
                    or "challenge" in f2_url
                    or "accounts.google.com" in f2_url
                    or "reauth" in f2_url
                ):
                    try:
                        # 1. Check if there is a password input
                        pwd_inp = f2.locator(
                            'input[type="password"], input[name="Passwd"]'
                        )
                        if (
                            await pwd_inp.count() > 0
                            and await pwd_inp.first.is_visible()
                        ):
                            is_cvc_box = "cvc" in f2_txt.lower() or "security code" in f2_txt.lower()
                            if not is_cvc_box:
                                self.log(
                                    "Detected Google password reauthentication; entering password..."
                                )
                                await pwd_inp.first.fill(acct_pwd)
                                await asyncio.sleep(0.4)
                                vbtns = f2.locator(
                                    'button:has-text("Verify"), button:has-text("Next"), button:has-text("Confirm"), button:has-text("Continue"), #passwordNext'
                                )
                                if (
                                    await vbtns.count() > 0
                                    and await vbtns.first.is_visible()
                                ):
                                    await vbtns.first.click()
                                else:
                                    await page.keyboard.press("Enter")
                                await asyncio.sleep(3)
                                continue

                        # 2. Check if there is a 2FA TOTP input
                        totp_inp = f2.locator('input#totpPin, input[name="totpPin"]')
                        if (
                            await totp_inp.count() > 0
                            and await totp_inp.first.is_visible()
                        ):
                            code = await self.ensure_totp_fresh()
                            if code:
                                self.log(
                                    f"Detected inline 2FA field; entering code: {code}"
                                )
                                await totp_inp.first.fill(code)
                                await page.keyboard.press("Enter")
                                await asyncio.sleep(3)
                                continue

                        # 3. Check if there is a CVC input
                        cvc_inp = f2.locator(
                            'input[autocomplete="cc-csc"], input[name="cvc"]'
                        )
                        if await cvc_inp.count() == 0 and (
                            "cvc" in f2_txt.lower() or "security code" in f2_txt.lower()
                        ):
                            cvc_inp = f2.locator(
                                'input[type="tel"], input[type="password"]'
                            )
                        if (
                            await cvc_inp.count() > 0
                            and await cvc_inp.first.is_visible()
                        ):
                            self.log(
                                f"Detected card security code (CVC) field; entering CVC: {cvc_code}"
                            )
                            await cvc_inp.first.fill(cvc_code)
                            await asyncio.sleep(0.5)
                            vbtns = f2.locator(
                                'button:has-text("Verify"), button:has-text("Confirm"), button:has-text("Continue"), button:has-text("Subscribe")'
                            )
                            if await vbtns.count() > 0:
                                await vbtns.first.click()
                            else:
                                await page.keyboard.press("Enter")
                            await asyncio.sleep(3)
                            continue

                        # 4. Check for a verification or continue button without visible inputs.
                        if "Verify it's you" in f2_txt:
                            vbtn = f2.locator(
                                'button:has-text("Verify"), button:has-text("Continue"), button:has-text("Next"), [role="button"]:has-text("Verify"), [role="button"]:has-text("Continue")'
                            ).first
                            if await vbtn.count() > 0 and await vbtn.is_visible():
                                self.log(
                                    f"Detected identity verification button ({await vbtn.inner_text()}), clicking..."
                                )
                                await vbtn.click()
                                await asyncio.sleep(2.5)
                    except PlaywrightError:
                        _LOGGER.debug("Optional browser action failed", exc_info=True)

                if f2_txt:
                    try:
                        ft_after = f2_txt
                        clean_after = " | ".join(
                            [l.strip() for l in ft_after.splitlines() if l.strip()]
                        )
                        if (
                            not any(error in clean_after.lower() for error in (
                                "something went wrong", "couldn't be completed",
                                "declined", "insufficient funds", "purchase failed",
                                "order is still being processed", "[or-", "[or_", "[bx-",
                            ))
                            and any(ok_kw in clean_after.lower() for ok_kw in (
                                "subscribed", "thank you for subscribing",
                                "your new membership is active",
                            ))
                        ):
                            self.log(
                                f"[OK] [{step_tag}] Detected subscription success confirmation!"
                            )
                            await self.save_screenshot(page, f"{step_tag}_after_click")
                            try:
                                dismiss_btn = f2.locator(
                                    'button:has-text("Not now"), [role="button"]:has-text("Not now"), button:has-text("No thanks"), button:has-text("Got it")'
                                ).first
                                if (
                                    await dismiss_btn.count() > 0
                                    and await dismiss_btn.is_visible()
                                ):
                                    self.log(
                                        f"[{step_tag}] Clicking Not now to close the dialog and synchronize order status..."
                                    )
                                    try:
                                        await dismiss_btn.click(
                                            force=True, timeout=3000
                                        )
                                    except PlaywrightError:
                                        _LOGGER.debug(
                                            "Optional browser action failed",
                                            exc_info=True,
                                        )
                                    await f2.evaluate(
                                        """() => {
                                        const els = Array.from(document.querySelectorAll('button, [role="button"], span, div'));
                                        const b = els.find(e => {
                                            const t = (e.innerText || '').trim();
                                            return t === 'Not now' || t === 'No thanks' || t === 'Got it';
                                        });
                                        if (b) {
                                            b.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                                            b.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
                                            b.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                                            b.click();
                                        }
                                    }"""
                                    )
                                    await asyncio.sleep(6.0)
                                else:
                                    await asyncio.sleep(5.0)
                            except PlaywrightError:
                                await asyncio.sleep(4.0)
                            return True
                        err_keywords = [
                            "Something went wrong",
                            "Couldn't be completed",
                            "Your payment was declined",
                            "Payment declined",
                            "Insufficient funds",
                            "[OR-",
                            "[OR_",
                            "OR_FGVEM",
                            "[BX-CPL-05]",
                            "Your order is still being processed",
                            "After your order is processed",
                            "Purchase failed",
                            "Something went wrong",
                            "couldn't be completed",
                            "Check that you have the correct country",
                            "declined",
                            "insufficient funds",
                        ]
                        if any(err_kw in clean_after for err_kw in err_keywords):
                            err_lines = [
                                line.strip()
                                for line in ft_after.splitlines()
                                if any(err_kw in line for err_kw in err_keywords)
                            ]
                            post_click_err = (
                                " | ".join(err_lines)
                                if err_lines
                                else clean_after[:300]
                            )
                            break
                    except PlaywrightError:
                        _LOGGER.debug("Optional browser action failed", exc_info=True)
            if post_click_err:
                break
            if wait_i >= 3 and not eacquire_present and not reauth_spinning:
                # Check the main page for a failure toast or membership welcome message before interpreting closure.
                try:
                    main_body_txt = (
                        await page.main_frame.inner_text("body", timeout=2000)
                    ).strip()
                except PlaywrightError:
                    main_body_txt = ""
                if (
                    "Purchase failed" in main_body_txt
                    or "Something went wrong" in main_body_txt
                    or "[BX-CPL-05]" in main_body_txt
                ):
                    post_click_err = "Detected a purchase failure message or an unsettled order ([BX-CPL-05])"
                    break
                if (
                    "Welcome to Google One" in main_body_txt
                    or "Your new membership is active" in main_body_txt
                ):
                    self.log(
                        f"[OK] [{step_tag}] Detected the Google One membership welcome page; subscription succeeded!"
                    )
                    await self.save_screenshot(page, f"{step_tag}_after_click")
                    await asyncio.sleep(4.0)
                    return True
                self.log(
                    f"[WARN] [{step_tag}] Checkout closed without a clear success confirmation; checking subscription status..."
                )
                await asyncio.sleep(5.0)
                await self.save_screenshot(page, f"{step_tag}_after_click")
                post_click_err = "Checkout closed without subscription confirmation; the order may be pending"
                break

        await self.save_screenshot(page, f"{step_tag}_after_click")
        if post_click_err:
            self.last_subscribe_error = post_click_err
            self.log(
                f"[ERROR] [{step_tag}] Google Play returned an error after Subscribe: {post_click_err[:300]}"
            )
            return False
        self.last_subscribe_error = "Timed out"
        self.log(
            f"[WARN] [{step_tag}] Timed out; dialog has not closed or confirmed (reauth_spinning={reauth_spinning})"
        )
        return False

    async def step2_layer1_12m(self, page):
        self.update_state(
            status="running",
            current_step=2,
            step_message="Step 2/5: Applying first 12-month free period (US$0.00 -> October 2027)...",
        )
        self.log(
            "=== Step 2/5: Applying first 12-month free period + YouTube 1M trial (US$0.00) ==="
        )

        play_txt = await self.inspect_play_subscriptions(page)
        if "2028" in play_txt:
            self.log("[OK] Play page mentions 2028; skipping Step 2.")
            return
        if any(
            k in play_txt
            for k in (
                "Oct 2027",
                "Nov 2027",
                "Dec 2027",
            )
        ):
            if "resubscribe" in play_txt.lower():
                await self.ensure_subscription_active(page)
            self.log(
                "[OK] Account already has the first 12-month free period; skipping Step 2!"
            )
            return

        if "resubscribe" in play_txt.lower():
            await self.ensure_subscription_active(page)

        sub_id = None
        if "Google AI" in play_txt or "Google One" in play_txt:
            sub_id = await self.get_current_sub_id(page)

        cfg_l1 = {
            "name": "Step2_Layer1_12M_YTFree_ZeroCost",
            "full_items": [
                [
                    [PREFIX + "g1.2tb.ai.subs_variant", 21],
                    None,
                    None,
                    None,
                    1,
                    [TOK_PRO5TB_12M],
                ],
                [
                    [PREFIX + "g1.youtube_premium_individual_add_on", 21],
                    None,
                    None,
                    None,
                    1,
                    [YT_FREE_TOK],
                ],
            ],
            "ear8": [None, 5, None, None, None, [sub_id]] if sub_id else None,
        }
        ok = await self.open_and_click_subscribe(page, cfg_l1, "step2_layer1_12m")
        if not ok:
            self.log(
                "[WARN] Step 2 checkout did not finish on the first attempt; retrying in 3 seconds (2/2)..."
            )
            await asyncio.sleep(3.0)
            ok = await self.open_and_click_subscribe(page, cfg_l1, "step2_layer1_12m")
        if not ok:
            raise RuntimeError(
                f"Step 2 (first 12-month free period US$0.00) failed: {self.last_subscribe_error}"
            )
        self.log(
            "[OK] Step 2 (first 12-month free period US$0.00) subscription complete! Waiting 45 seconds for Google Play settlement..."
        )
        await asyncio.sleep(45)

    async def step3_layer2_6mss(self, page):
        self.update_state(
            status="running",
            current_step=3,
            step_message="Step 3/5: Applying second 6-month free period (US$0.00 -> 18 months / April 2028)...",
        )
        self.log(
            "=== Step 3/5: Applying second 6-month free period (S25 Pro 6M + YT 1M trial, US$0.00) ==="
        )

        play_txt = await self.inspect_play_subscriptions(page)
        if any(
            k in play_txt
            for k in (
                "Apr 2028",
                "May 2028",
                "Jun 2028",
                "Oct 2028",
                "Nov 2028",
                "Dec 2028",
            )
        ):
            if "resubscribe" in play_txt.lower() and not any(
                k in play_txt
                for k in (
                    "Oct 2028",
                    "Nov 2028",
                    "Dec 2028",
                )
            ):
                await self.ensure_subscription_active(page)
            self.log(
                "[OK] Account shows a 2028 expiration; skipping Step 3."
            )
            return

        if "resubscribe" in play_txt.lower():
            await self.ensure_subscription_active(page)

        sub_id = await self.get_current_sub_id(page)

        cfg_l2 = {
            "name": "Step3_Layer2_6MSS_YTFree_ZeroCost",
            "full_items": [
                [
                    [PREFIX + "g1.2tb.ai.subs_variant", 21],
                    None,
                    None,
                    None,
                    1,
                    [TOK_PRO5TB_6M_SS],
                ],
                [
                    [PREFIX + "g1.youtube_premium_individual_add_on", 21],
                    None,
                    None,
                    None,
                    1,
                    [YT_FREE_TOK],
                ],
            ],
            "ear8": [None, 5, None, None, None, [sub_id]] if sub_id else None,
        }
        ok = await self.open_and_click_subscribe(page, cfg_l2, "step3_layer2_6mss")
        if not ok:
            self.log(
                "[WARN] Step 3 checkout did not finish on the first attempt; retrying in 3 seconds (2/2)..."
            )
            await asyncio.sleep(3.0)
            ok = await self.open_and_click_subscribe(page, cfg_l2, "step3_layer2_6mss")
        if not ok:
            raise RuntimeError(
                f"Step 3 (second 6-month free period US$0.00) failed: {self.last_subscribe_error}"
            )
        self.log(
            "[OK] Step 3 (second 6-month free period US$0.00 -> 18 months) applied! Waiting 45 seconds to synchronize the subscription ID..."
        )
        await asyncio.sleep(45)

    async def step4_layer3_6m(self, page):
        self.update_state(
            status="running",
            current_step=4,
            step_message="Step 4/5: Applying third 6-month free period (US$0.00 -> 24 months / October 2028)...",
        )
        self.log(
            "=== Step 4/5: Applying third 6-month free period (Pixel 6M + YT 1M trial, US$0.00 -> 24 months) ==="
        )

        play_txt = await self.inspect_play_subscriptions(page)
        if any(
            k in play_txt
            for k in (
                "Oct 2028",
                "Nov 2028",
                "Dec 2028",
            )
        ):
            self.log(
                "[OK] Account shows a late-2028 expiration; skipping Step 4."
            )
            return

        if "resubscribe" in play_txt.lower():
            await self.ensure_subscription_active(page)

        sub_id = await self.get_current_sub_id(page)

        ok = False
        for tag, tok in [
            ("step4_layer3_6m", TOK_PRO5TB_6M),
            ("step4_layer3_6m2", TOK_PRO5TB_6M_2),
        ]:
            cfg_l3 = {
                "name": tag,
                "full_items": [
                    [
                        [PREFIX + "g1.2tb.ai.subs_variant", 21],
                        None,
                        None,
                        None,
                        1,
                        [tok],
                    ],
                    [
                        [PREFIX + "g1.youtube_premium_individual_add_on", 21],
                        None,
                        None,
                        None,
                        1,
                        [YT_FREE_TOK],
                    ],
                ],
                "ear8": [None, 5, None, None, None, [sub_id]] if sub_id else None,
            }
            ok = await self.open_and_click_subscribe(page, cfg_l3, tag)
            if ok:
                break
            await asyncio.sleep(3.0)

        if not ok:
            raise RuntimeError(
                f"Step 4 (third 6-month free period US$0.00) failed: {self.last_subscribe_error}"
            )

        self.log(
            "[OK] Step 4 (third 6-month free period US$0.00 -> 24 months) applied! Waiting 40 seconds to synchronize the expiration date..."
        )
        await asyncio.sleep(40)
        play_after = await self.inspect_play_subscriptions(page)
        if "2028" not in play_after:
            raise RuntimeError(
                "Step 4 dialog closed, but the Play subscription expiration date did not reach 2028!"
            )
        self.log("[OK] Step 4 checkout completed; Play page mentions 2028.")

    async def step5_cancel_and_verify(self, page):
        self.update_state(
            status="running",
            current_step=5,
            step_message="Step 5/5: Cancelling automatic renewal and capturing the 2028 expiration date...",
        )
        self.log(
            "=== Step 5/5: Cancelling automatic renewal and capturing the final 2028 expiration date ==="
        )

        await self.safe_goto(
            page,
            "https://play.google.com/store/account/subscriptions?hl=en-US",
            timeout=40000,
        )
        await asyncio.sleep(4)
        play_txt = await page.inner_text("body")

        if (
            self.auto_cancel
            and not re.search(r"\b(?:cancelled|canceled|resubscribe|auto-renewal\s+(?:is\s+)?off)\b", play_txt, re.IGNORECASE)
        ):
            mbtn = page.get_by_text("Manage", exact=True)
            for i in range(await mbtn.count()):
                if await mbtn.nth(i).is_visible():
                    await mbtn.nth(i).click()
                    break

            for _ in range(15):
                await asyncio.sleep(0.5)
                cbtn = page.get_by_text("Cancel subscription", exact=True)
                if await cbtn.count() > 0 and await cbtn.first.is_visible():
                    await cbtn.first.click()
                    break

            await asyncio.sleep(2.5)
            # Handle the optional retention screen (No thanks / Continue).
            no_thanks = page.locator(
                'button:has-text("No thanks"), [role="button"]:has-text("No thanks")'
            )
            if await no_thanks.count() > 0 and await no_thanks.first.is_visible():
                await no_thanks.first.click()
                await asyncio.sleep(2.0)

            # Click a visible radio button (and/or reason text) to enable Continue.
            await page.evaluate(
                """() => {
                const radios = Array.from(document.querySelectorAll('[role="radio"], input[type="radio"]')).filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                if (radios.length > 0) {
                    const target = radios[radios.length - 1];
                    target.click();
                    if (target.parentElement) target.parentElement.click();
                }
            }"""
            )
            await asyncio.sleep(0.5)
            vis_radios = page.locator(
                '[role="radio"]:visible, input[type="radio"]:visible'
            )
            if await vis_radios.count() > 0:
                try:
                    await vis_radios.last.click(force=True)
                except PlaywrightError:
                    _LOGGER.debug("Optional browser action failed", exc_info=True)
            reason = page.locator(
                "text=/Decline to answer|Prefer not to say|Other reason|No longer needed/i"
            ).first
            if await reason.count() > 0 and await reason.is_visible():
                try:
                    await reason.click(force=True)
                except PlaywrightError:
                    _LOGGER.debug("Optional browser action failed", exc_info=True)

            await asyncio.sleep(0.8)
            cnext = page.locator(
                'button:has-text("Continue"):not([disabled]), [role="button"]:has-text("Continue"):not([aria-disabled="true"])'
            )
            for i in range(await cnext.count()):
                if await cnext.nth(i).is_visible():
                    await cnext.nth(i).click(timeout=8000)
                    break
            await asyncio.sleep(2.5)

            final_cbtn = page.get_by_text("Cancel subscription", exact=True)
            fcnt = await final_cbtn.count()
            for i in range(fcnt - 1, -1, -1):
                if await final_cbtn.nth(i).is_visible():
                    self.log("Clicking the final Cancel subscription confirmation...")
                    await final_cbtn.nth(i).click(timeout=8000)
                    break
            await asyncio.sleep(4)
            if len(page.context.pages) > 1:
                await self.pass_reauth_on_page(page.context.pages[-1])
            await asyncio.sleep(3)

        # Final verification on Play Subscriptions
        await self.safe_goto(
            page,
            "https://play.google.com/store/account/subscriptions?hl=en-US",
            timeout=40000,
        )
        await asyncio.sleep(3.5)
        final_shot = await self.save_screenshot(page, "step5_final_verified")
        final_txt = " | ".join(
            [
                l.strip()
                for l in (await page.inner_text("body")).splitlines()
                if l.strip()
            ]
        )
        self.log(f"Final Play subscription page status: {final_txt[:400]}")

        plans = set(
            re.findall(
                r"Google AI (?:Pro|Ultra)(?:\s*\(\s*\d+\s*TB\s*\))?",
                final_txt,
                re.IGNORECASE,
            )
        )
        if len(plans) != 1:
            raise RuntimeError("Final verification could not identify one Google AI subscription plan")
        plan_found = plans.pop()

        expiry = _subscription_expiry_date(final_txt)
        exp_str = expiry.isoformat()

        if self.auto_cancel and not re.search(
            r"\b(?:cancelled|canceled|resubscribe|auto-renewal\s+(?:is\s+)?off)\b",
            final_txt,
            re.IGNORECASE,
        ):
            raise RuntimeError("Final verification found no evidence that renewal was cancelled")

        cancellation_status = " | Renewal cancelled" if self.auto_cancel else ""

        self.update_state(
            status="completed",
            current_step=5,
            step_message=f"[OK] Subscription verified! Plan: {plan_found} | Expiration date: {exp_str}{cancellation_status}",
            expiry_date=exp_str,
            last_screenshot=final_shot,
        )
        self.log(
            f"Account {self.account['email']} subscription verified: {plan_found}; expiration: {exp_str}{cancellation_status}"
        )

    # Keep step7_cancel_and_verify alias for compatibility
    step7_cancel_and_verify = step5_cancel_and_verify

    async def verify_ip_environment(self, page):
        """Check current browser exit IP and country before touching Google."""
        for probe_url in ("https://ipinfo.io/json", "https://ipwho.is/"):
            try:
                await page.goto(probe_url, wait_until="domcontentloaded", timeout=16000)
                txt = await page.inner_text("body")
                data = json.loads(txt)
                if not isinstance(data, dict):
                    raise TypeError("IP probe returned a non-object response")
                if "ipwho" in probe_url and not data.get("success", True):
                    continue
                ip = data.get("ip", "Unknown")
                cc = data.get("country_code") or data.get("country") or "??"
                city = data.get("city", "")
                connection = data.get("connection")
                org = (
                    connection.get("org")
                    if isinstance(connection, dict)
                    else data.get("org", "")
                ) or ""
                if not all(isinstance(value, str) for value in (ip, cc, city, org)):
                    raise ValueError("IP probe returned invalid field types")
                cc = cc.upper()
                ip_badge = f"{cc} {ip[:22]}" + (f" ({city})" if city else "")
                self.update_state(ip_info=ip_badge)
                self.log(
                    f"[OK] Profile exit IP check passed: {ip} ({cc} {city}) | Provider: {org[:35]}"
                )
                return
            except (PlaywrightError, TypeError, ValueError):
                if self.proxy_bridge:
                    self.proxy_bridge.rotate_session()
                continue
        self.log("[WARN] IP check is slow; continuing...")

    async def run_pipeline(self, start_step: int = 1):
        cnum_check = re.sub(r"\s+", "", self.card.get("number") or "")
        expm_check = (self.card.get("exp_month") or "").strip()
        expy_check = (self.card.get("exp_year") or "").strip()
        cvc_check = (self.card.get("cvc") or "").strip()
        if start_step <= 4 and (
            len(cnum_check) < 12 or not expm_check or not expy_check or not cvc_check
        ):
            err_card = "Enter complete payment card details (number, expiration month/year, and CVC) in Global Configuration and save first!"
            self.log(f"[ERROR] {err_card}")
            self.update_state(status="error", step_message=f"[ERROR] {err_card}")
            return

        user_data_dir = get_profile_dir(self.account["id"], self.account["email"])
        resolved_proxy = resolve_account_proxy(
            self.account, self.target_region, self.proxy_template
        )
        parsed_proxy = parse_proxy_string(resolved_proxy)

        chrome_args = [
            CHROME_PATH,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-component-update",
            "--disable-domain-reliability",
            "--no-pings",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--enforce-webrtc-ip-permission-check",
            "--start-maximized",
            "--lang=en-US",
        ]

        try:
            if parsed_proxy:
                if parsed_proxy.get("username"):
                    bridge_port = self.port + 200
                    self.proxy_bridge = LocalHttpProxyAuthBridge(
                        bridge_port, parsed_proxy
                    )
                    await self.proxy_bridge.start()
                    chrome_args.append(f"--proxy-server=http://127.0.0.1:{bridge_port}")
                    self.log(
                        f"[INFO] For [{self.account['id']}] attached dedicated residential proxy tunnel -> {parsed_proxy['host']}:{parsed_proxy['port']} (session: {parsed_proxy['username'][:28]}...)"
                    )
                else:
                    p_url = f"{parsed_proxy['scheme']}://{parsed_proxy['host']}:{parsed_proxy['port']}"
                    chrome_args.append(f"--proxy-server={p_url}")
                    self.log(
                        f"[INFO] For [{self.account['id']}] attached dedicated proxy -> {p_url}"
                    )
            else:
                self.log(
                    f"Starting Chrome profile (port: {self.port}, no dedicated proxy configured; using system network)..."
                )

            chrome_args.append("about:blank")

            await asyncio.to_thread(_stop_processes_listening_on_port, self.port)
            await asyncio.sleep(0.8)

            self.proc = await asyncio.create_subprocess_exec(
                *chrome_args, start_new_session=True
            )
            await asyncio.sleep(2.8)
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self.port}"
                )
                context = browser.contexts[0]
                await context.add_init_script("window.__INIT_TEST = null;\n" + HOOK_JS)

                page = context.pages[0] if context.pages else await context.new_page()
                await page.set_viewport_size({"width": 1400, "height": 950})

                await self.verify_ip_environment(page)

                steps = [
                    (1, lambda: self.step1_login(page)),
                    (2, lambda: self.step2_layer1_12m(page)),
                    (3, lambda: self.step3_layer2_6mss(page)),
                    (4, lambda: self.step4_layer3_6m(page)),
                    (5, lambda: self.step5_cancel_and_verify(page)),
                ]

                for step_num, step_fn in steps:
                    self.check_stop()
                    if step_num < start_step:
                        continue
                    await step_fn()

                await browser.close()
        except Exception as e:
            _LOGGER.exception("Account automation failed")
            err_msg = str(e)
            self.log(f"[ERROR] Execution interrupted: {err_msg}")
            self.update_state(
                status="error",
                step_message=f"[ERROR] Step {self.account.get('current_step', 1)} error: {err_msg[:120]}",
            )
        finally:
            try:
                if self.proxy_bridge:
                    await self.proxy_bridge.stop()
            finally:
                if self.proc:
                    await _stop_process_group(self.proc)
