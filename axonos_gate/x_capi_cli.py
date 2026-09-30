#!/usr/bin/env python3
"""Secret-safe offline validation and synthetic payload demonstration."""

import argparse
import json

try:
    from . import x_capi, x_capi_worker
except ImportError:
    import x_capi
    import x_capi_worker


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("validate", "status", "demo-payload", "clear-pause")
    )
    parser.add_argument(
        "--require-listener", action="store_true",
        help="also require both locked local worker sockets",
    )
    args = parser.parse_args(argv)
    if args.command == "validate":
        status = x_capi_worker.worker_readiness()
        print(json.dumps(status, indent=2, sort_keys=True))
        ready = status["worker_ready"] and (
            status["ingest_listener_present"]
            and status["consent_listener_present"]
            if args.require_listener else True
        )
        return 0 if ready else 2
    if args.command == "clear-pause":
        cleared = x_capi_worker.clear_pause()
        print(json.dumps({"pause_cleared": cleared}, sort_keys=True))
        return 0 if cleared else 2
    if args.command == "status":
        status = x_capi_worker.local_status()
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0 if status["mode"] != "live" or status["worker_db_available"] else 2
    payload = x_capi_worker.build_payload(
        {
            "conversion_timestamp_ms": 1789387200000,
            "event_id": "synthetic-event-id",
            "twclid": "synthetic-click-id",
            "conversion_id": "00000000-0000-4000-8000-000000000001",
        }
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
