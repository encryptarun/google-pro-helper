<div align="center">

# Google AI Pro Emulator Helper

Run the Google AI Pro (5 TB) offer workflow in BlueStacks Air on a Mac.

**[Requirements](#requirements)** · **[First run](#first-run)** · **[Run the steps](#run-the-steps)** · **[Troubleshooting](#troubleshooting)**

</div>

> [!IMPORTANT]
> Offers and trial lengths depend on the account and Play checkout. Read the amount and next payment date before confirming anything. The script pauses at checkout so you tap **Subscribe** yourself.

## Requirements

- **Mac:** Python 3.10 or later and Node.js with npm.
- **Emulator:** BlueStacks Air installed at `/Applications/BlueStacks.app` and running.
- **Android access:** ADB connected to BlueStacks Air, root access through Magisk, and the MagiskFrida module running. The setup checks these. It matches the Mac Frida client to the module's server version.
- **Apps:** Google One and Google Play Store installed in BlueStacks Air. Sign in to the intended Google account and add a payment method before checkout.
- **Subscription:** Start with step 1 on an account without this Pro subscription. Steps 2–4 need the earlier Pro subscription to be active with renewal enabled. Check this in Play Store before each extension.

## First run

Open BlueStacks Air, then double-click [setup_environment.command](./setup_environment.command). You can also run it from Terminal inside the `emulator` directory.

```bash
./setup_environment.command
```

Setup creates a local `.venv`, installs the matching Frida client and Java bridge, then checks ADB, root, Frida, the billing module, and Play Store. Continue when it says **Environment check passed**.

## Run the steps

Double-click the launchers in order. Each one opens a Terminal window and prints its result. Run the next purchase step only after checking the previous result and subscription date.

| Step | Launcher | What it does |
| --- | --- | --- |
| Check | [0_environment_check.command](./0_environment_check.command) | Recheck BlueStacks Air, root, Frida, and Play Store. |
| Offers | [1_list_offers.command](./1_list_offers.command) | List available Pro offers without opening checkout. |
| 1 | [2_pro_12_months_step_1.command](./2_pro_12_months_step_1.command) | Open the 12-month Pro offer. |
| 2 | [3_pro_add_6_months_step_2.command](./3_pro_add_6_months_step_2.command) | Try the first 6-month extension. |
| 3 | [4_pro_add_6_months_step_3.command](./4_pro_add_6_months_step_3.command) | Try the second 6-month extension. |
| Optional | [5_pro_extra_6_months.command](./5_pro_extra_6_months.command) | Try the remaining 6-month offer. |

At checkout, inspect the displayed price and trial dates. If the script finds a zero price or a free trial, it asks you to tap **Subscribe** in BlueStacks Air, then press **Enter** in Terminal. It then opens Play subscriptions. Tap your Google AI Pro subscription there and press **Enter** again to let the script read that screen.

Check the next payment date in **Play Store → Profile → Payments & subscriptions → Subscriptions** after every step. A checkout error, an unexpected price, or a date that did not move later means you should stop that sequence.

## Account file and manual commands

The numbered launchers use the account currently selected in Play Store. For multiple accounts, put one account on each line of a text file. Separate fields with four hyphens.

```text
email----password----recovery email----2FA secret----card number----expiry----CVC----cardholder----ZIP code
```

You can leave later fields blank. Run the script from the `emulator` directory after setup.

```bash
.venv/bin/python pro.py accounts.txt
.venv/bin/python pro.py --account "name@example.com:password:2FA-secret" --plan pro12
```

The available plans are `pro12`, `pro6`, `pro6b`, and `pro6c`. The default is `pro12`. Extension commands use `--stack` to pass the existing subscription token to Play Billing. [instructions.txt](./instructions.txt) has the equivalent manual commands and expected date checks.

## Troubleshooting

- **No device found:** open BlueStacks Air and enable its ADB connection. Setup expects the bundled `hd-adb` at `/Applications/BlueStacks.app/Contents/MacOS/hd-adb` and a device at `127.0.0.1:5555`.
- **No root or Frida server:** check Magisk and the MagiskFrida module in BlueStacks Air, then rerun setup.
- **Google One did not start:** install and open Google One in BlueStacks Air, then retry.
- **Purchase token unavailable:** check that the existing Pro subscription belongs to the selected Play account and is active before running an extension.
- **Play checkout error:** if Play shows “Something went wrong on our end,” the script reports `play_checkout_error`. Check the subscription state in Play Store before another attempt. A canceled subscription may keep benefits until its end date without being eligible for an extension.
- **Verification did not find Pro:** open the Pro subscription detail in Play Store and press Enter only when that screen is visible.

## Local data

The `.venv/` and `.frida-bridge/` directories are generated by setup. `pro-report/` holds local run results and may contain account details and offer tokens. These directories, screenshots, and `.DS_Store` files are excluded from Git. Keep account files and reports private.
