# android

Tools for inspecting and (carefully) driving your **own** Android device from a
PC over adb. The audit tools are strictly read-only: they find and report,
they never change the device. Python 3.9+, stdlib only; `adb` must be in PATH
(or set `ANDROID_ADB`). Select a device with `ANDROID_SERIAL` / `--serial`.

| Script | What it does |
|---|---|
| `android_audit.py` | Hardening audit + anomaly monitor: snapshot/baseline diff of boot/build props, security settings, AppOps, listeners, device admins, packages and risky permissions; cron-friendly `watch` and weekly `deep` check. |
| `android_listeners.py` | Maps open TCP listeners to UID/package and classifies them (loopback / wildcard / own / foreign). |
| `app_inventory.py` | Read-only app inventory with sizes and granted permissions; lists OEM packages matching prefixes you provide as disable candidates. |
| `phone_transfer.py` | File transfer PC <-> phone over adb with SHA-256 verification (useful when USB tethering and MTP exclude each other). |
| `phone_idle_check.sh` | "Is the phone in use?" - exit 0 only when the screen is off, so automation leaves the user alone. |
| `phone_ui.py` | Minimal text UI driver (`uiautomator dump` + `input`): list elements, tap, type, swipe, wait. |
| `connection_log.py` | Append-only JSONL log of USB/Wi-Fi connect/disconnect events (SSID only if explicitly enabled). |
| `on_device_event.py` | Entry point for udev/NetworkManager hooks: logs the event and triggers the note pull from `../second-brain/`. |
| `termux_background_job.sh` | Runs a Python job inside Termux with wake lock, taskset/nice/cpulimit throttling and hints for the phantom process killer. |

Use these only on devices you own or are authorised to administer.

Author: Lukas Weißmann - License: MIT
