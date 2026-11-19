# Standalone participant DNP3 master 
import argparse
import asyncio
import json
from protocol import exchange

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=20000)
    parser.add_argument("--kind", choices=["binary", "analog"])
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--value", type=float, default=0)
    parser.add_argument("--sbo", action="store_true", help="Select before operate")
    args = parser.parse_args()
    if args.kind == "binary" and args.value not in (0, 1):
        parser.error("Binary --value must be 0 or 1")
    result = asyncio.run(exchange(args.host, args.port, args.kind, args.index, bool(args.value) if args.kind == "binary" else args.value, args.sbo))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "SUCCESS" else 1)
