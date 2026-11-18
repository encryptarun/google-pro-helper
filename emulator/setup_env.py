"""Prepare the macOS environment for BlueStacks Air and check the emulator.

Run with python3 setup_env.py or double-click setup_environment.command.
"""

import os
import platform
import subprocess
import sys
import venv
from pathlib import Path

BASE = Path(__file__).resolve().parent
BLUESTACKS_APP = Path("/Applications/BlueStacks.app")
ADB = BLUESTACKS_APP / "Contents/MacOS/hd-adb"
VENV_PYTHON = BASE / ".venv/bin/python"
DEVICE = "127.0.0.1:5555"
BRIDGE_DIR = BASE / ".frida-bridge"


def step(msg):
    print(f"\n▶ {msg}", flush=True)


def ok(msg):
    print(f"   ✅ {msg}", flush=True)


def die(msg):
    print(f"\n❌ {msg}", flush=True)
    return 1


def check_mac():
    step("Checking Mac and BlueStacks Air")
    if platform.system() != "Darwin":
        return die("This setup is for macOS.")
    if sys.version_info < (3, 10):
        return die("Python 3.10 or later is required. Install it with: brew install python")
    if not ADB.is_file():
        return die(f"BlueStacks Air ADB was not found at {ADB}")
    ok(f"BlueStacks Air: {BLUESTACKS_APP}")
    ok(f"Python: {platform.python_version()}")
    return 0


def install_frida():
    step("Preparing Frida")
    server = subprocess.run(
        [str(ADB), "-s", DEVICE, "shell", "su -c /data/adb/modules/magisk-frida/bin/frida-server --version"],
        capture_output=True, text=True, timeout=15,
    )
    version = server.stdout.strip()
    if server.returncode or not version.startswith("17."):
        return die("Could not read a Frida 17 server version from the MagiskFrida module.")
    if not VENV_PYTHON.is_file():
        venv.create(BASE / ".venv", with_pip=True)
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", f"frida=={version}"],
        capture_output=True, text=True,
    )
    if result.returncode:
        print((result.stderr or result.stdout).strip()[-800:])
        return die("Could not install Frida in emulator/.venv")
    bridge = subprocess.run(
        ["npm", "install", "--prefix", str(BRIDGE_DIR), "frida-java-bridge",
         "--fetch-retries=0", "--fetch-timeout=15000"],
        capture_output=True, text=True,
    )
    if bridge.returncode:
        bridge = subprocess.run(
            ["npm", "install", "--prefix", str(BRIDGE_DIR), "frida-java-bridge",
             "--registry=https://registry.npmmirror.com", "--fetch-retries=0",
             "--fetch-timeout=15000"],
            capture_output=True, text=True,
        )
    if bridge.returncode:
        print((bridge.stderr or bridge.stdout).strip()[-800:])
        return die("Could not download frida-java-bridge. Check npm network access and retry.")
    ok(f"Frida {version} and Java bridge ready")
    return 0


def connect_emulator():
    step("Checking BlueStacks Air connection")
    subprocess.run([str(ADB), "connect", DEVICE], capture_output=True, text=True, timeout=10)
    result = subprocess.run([str(ADB), "devices"], capture_output=True, text=True, timeout=10)
    connected = [line.split("\t")[0] for line in result.stdout.splitlines()[1:]
                 if "\tdevice" in line]
    if DEVICE not in connected:
        return die("BlueStacks Air is not connected. Open the app and enable ADB access, then retry.")
    ok(f"Connected: {DEVICE}")
    return 0


def run_check():
    step("Checking root, Frida server, and billing module")
    env = dict(os.environ, ADB_PATH=str(ADB), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [str(VENV_PYTHON), str(BASE / "pro.py"), "--check", "--serial", DEVICE],
        cwd=BASE, env=env, capture_output=True, text=True,
    )
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if output:
        print(output)
    if result.returncode:
        return die("Environment check failed. See the error above; this flow needs rooted BlueStacks Air.")
    ok("Ready. Open 2_pro_12_months_step_1.command to start.")
    return 0


def main():
    for check in (check_mac, connect_emulator, install_frida, run_check):
        if check():
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
