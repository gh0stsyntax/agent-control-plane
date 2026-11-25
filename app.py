"""AEAP Reference App starten:  python app.py  [--port 8710] [--no-browser] [--reset]"""

import argparse
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aeap.server import serve  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="AEAP Reference Implementation")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "aeap.db"))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reset", action="store_true", help="Datenbank beim Start zuruecksetzen")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    httpd, platform = serve(args.db, args.host, args.port)
    if args.reset:
        platform.reset()
    url = f"http://{args.host}:{httpd.server_address[1]}/"
    print("=" * 64)
    print(" AEAP · Agentic Enterprise Automation Platform (Reference App)")
    print(f" UI:        {url}")
    print(f" Datenbank: {args.db}")
    print(" Beenden:   Strg+C")
    print("=" * 64)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
