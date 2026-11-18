"""List all offers for the specified product IDs.

Usage: python query_sku.py g1.2tb.ai.subs_variant [other SKUs...]
"""
import json
import sys
import threading
import time

import frida

import pro


def main():
    skus = sys.argv[1:] or ["g1.2tb.ai.subs_variant"]
    pro.ADB = pro.find_adb()
    pro.pick_device()
    pro.sh("forward", "tcp:27042", "tcp:27042", check=True)
    dev = frida.get_device_manager().add_remote_device("127.0.0.1:27042")
    pid = 0
    for p in dev.enumerate_processes():
        if "subscriptions.red" in p.name or p.name == "Google One":
            pid = p.pid
            break
    if not pid:
        raise SystemExit("Google One process not found")

    js = pro.build_js("pro12", True)
    js = js.replace('var PRODUCTS=["g1.2tb.ai.subs_variant"];', 'var PRODUCTS=' + json.dumps(skus) + ';', 1)

    sess = dev.attach(pid)
    done = threading.Event()
    offers = []

    def on_msg(m, d):
        p = m.get("payload") or {}
        if p.get("ev") == "offer":
            offers.append(p)
        elif p.get("ev") in ("list_only", "no_trial", "query_err", "no_offers", "pd"):
            if p.get("ev") != "pd":
                done.set()

    sc = sess.create_script(pro.compile_js(js))
    sc.on("message", on_msg)
    sc.load()
    done.wait(timeout=25)
    time.sleep(1)

    print(f"\nOffers for {', '.join(skus)}")
    if not offers:
        print("  No offers returned")
    for o in offers:
        micros = str(o.get("micros"))
        price = "Free" if micros == "0" else f"${int(micros)/1_000_000:.2f}"
        print(f"  offerId={str(o.get('id')):22s} {price:>9s}  {o.get('period')}   [{o.get('title') or o.get('name') or ''}]")
        if micros == "0":
            print(f"      token={o.get('token')}")
    try:
        sess.detach()
    except Exception:
        pass


if __name__ == "__main__":
    main()
