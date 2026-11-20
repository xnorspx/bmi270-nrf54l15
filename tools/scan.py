"""Scan for nearby BLE devices and show whether the collector is advertising."""

from __future__ import annotations

import asyncio
import sys

from bleak import BleakScanner

TARGET = "KneeChe"


async def main() -> int:
    print("scanning 6s ...")
    found = await BleakScanner.discover(timeout=6.0)
    if not found:
        print("no BLE devices found at all")
        return 1

    hit = False
    for d in sorted(found, key=lambda x: x.name or ""):
        name = d.name or "(no name)"
        adv = getattr(d, "advertisement_data", None)
        rssi = getattr(adv, "rssi", None) if adv else None
        rssi_txt = f"{rssi:4d}" if isinstance(rssi, int) else "   ?"
        mark = "  <-- collector" if TARGET.lower() in name.lower() else ""
        print(f"  {d.address}  rssi={rssi_txt}  {name}{mark}")
        if TARGET.lower() in name.lower():
            hit = True

    print()
    if hit:
        print("RESULT: collector is advertising - firmware runs, console is the problem")
        return 0
    print("RESULT: collector NOT seen - firmware is not running (or crashed before BT)")
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
