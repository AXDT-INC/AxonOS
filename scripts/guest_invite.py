#!/usr/bin/env python3
"""
Demo-invite administration for AxonOS guest mode.

Mints, lists and revokes the single-use (or capped-use) invite tokens that are
the only way to start a wallet-free demo session. Talks to the invite tables
directly, so it works on a host with no gate HTTP access.

This is the OPERATOR path. The everyday path is a team member minting their own
links from the browser (POST /api/auth/guest-invite), authorized by the
invite-minter list rather than by an infra secret. Use this when there is no
signed-in wallet available -- a host shell, a runbook, CI.

The minted token is printed exactly once: only its sha256 is stored, so a lost
token cannot be recovered -- revoke it and mint another.

Requires AXGT_CHALLENGE_DB_URL. Minting additionally requires
AXONOS_GUEST_MODE_ENABLED=true; list/revoke remain available during an emergency
feature shutdown.

Examples:
  scripts/guest_invite.py mint --label "acme-corp demo" --minutes 30
  scripts/guest_invite.py mint --label "conference booth" --max-uses 25 --base-url https://axonconsole.io
  scripts/guest_invite.py list
  scripts/guest_invite.py revoke --token-hash 9f86d081...
"""

import argparse
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (_REPO_ROOT, _REPO_ROOT / "axonos_gate"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

try:
    import guest_mode
except ImportError:
    try:
        from axonos_gate import guest_mode
    except ImportError as exc:
        print(f"Error: could not import guest_mode: {exc}", file=sys.stderr)
        sys.exit(1)


def _fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _preflight(*, require_enabled: bool) -> None:
    if not os.getenv("AXGT_CHALLENGE_DB_URL"):
        _fail("AXGT_CHALLENGE_DB_URL is not set.")
    if require_enabled and not guest_mode.guest_mode_enabled():
        _fail(
            "Guest mode is disabled. Set AXONOS_GUEST_MODE_ENABLED=true "
            "(on the gate too) before issuing demo invites. List and revoke "
            "remain available while it is disabled."
        )


def _csv(value):
    if not value:
        return None
    return [chunk.strip() for chunk in value.split(",") if chunk.strip()]


def _stamp(epoch) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(epoch)))


def cmd_mint(args) -> int:
    result = guest_mode.mint_invite(
        label=args.label,
        max_uses=args.max_uses,
        session_minutes=args.minutes,
        allowed_profiles=_csv(args.profiles),
        allowed_templates=_csv(args.templates),
        ttl_hours=args.ttl_hours,
        # A sponsor wallet makes the invite subject to that member's quotas and
        # attributes it to them; without one it is an unattributed operator mint.
        created_by=(args.sponsor or "guest_invite_cli"),
    )
    if not result.get("ok"):
        _fail(result.get("error") or result.get("error_code") or "mint failed")

    print("Demo invite created.\n")
    print(f"  label            {result.get('label') or '-'}")
    print(f"  minted by        {result.get('created_by') or '-'}")
    print(f"  demo length      {result['session_minutes']} min")
    print(f"  uses             {result['max_uses']}")
    print(f"  hardware tiers   {', '.join(result['allowed_profiles'])}")
    print(f"  environments     {', '.join(result['allowed_templates']) or 'any'}")
    print(f"  link expires     {_stamp(result['expires_at'])}")
    print(f"  token hash       {result['token_hash']}")
    print("\n  Demo link (shown once -- only its hash is stored):\n")
    print(f"    {args.base_url.rstrip('/')}/?invite={result['token']}\n")
    return 0


def cmd_list(args) -> int:
    result = guest_mode.list_invites(limit=args.limit)
    if not result.get("ok"):
        _fail(result.get("error") or result.get("error_code") or "list failed")
    invites = result["invites"]
    if not invites:
        print("No demo invites.")
        return 0
    print(f"{'STATE':<9} {'USES':<8} {'MIN':<5} {'EXPIRES':<17} {'LABEL':<20} {'BY':<14} HASH")
    for inv in invites:
        if inv["revoked"]:
            state = "revoked"
        elif not inv["usable"]:
            state = "spent" if inv["uses"] >= inv["max_uses"] else "expired"
        else:
            state = "usable"
        uses = f"{inv['uses']}/{inv['max_uses']}"
        label = (inv["label"] or "-")[:20]
        by = str(inv.get("created_by") or "-")
        by = f"{by[:6]}..{by[-4:]}" if by.startswith("0x") and len(by) > 12 else by[:14]
        print(
            f"{state:<9} {uses:<8} {inv['session_minutes']:<5} "
            f"{_stamp(inv['expires_at']):<17} {label:<20} {by:<14} {inv['token_hash'][:16]}..."
        )
    print(f"\n{result['count']} invite(s).")
    return 0


def cmd_revoke(args) -> int:
    target = args.token or args.token_hash
    if not target:
        _fail("pass --token or --token-hash")
    result = guest_mode.revoke_invite(target)
    if not result.get("ok"):
        _fail(result.get("error") or result.get("error_code") or "revoke failed")
    print(f"Revoked invite {result['token_hash'][:16]}...")
    targeted = int(result.get("sessions_targeted") or 0)
    stopped = int(result.get("sessions_stopped") or 0)
    if targeted:
        print(f"Stopped {stopped}/{targeted} running demo session(s).")
    else:
        print("No running demo session was attached to this invite.")
    if result.get("cleanup_pending"):
        print("Runtime cleanup is pending and will be retried by the session reaper.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Administer AxonOS wallet-free demo invites.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_mint = sub.add_parser("mint", help="create a demo invite and print its link")
    p_mint.add_argument("--label", default="", help="who this demo link is for (audit only)")
    p_mint.add_argument("--max-uses", type=int, default=1, help="how many demos this link may start (default: 1)")
    p_mint.add_argument("--minutes", type=int, default=None, help="demo length; default from AXONOS_GUEST_SESSION_MINUTES")
    p_mint.add_argument("--profiles", default=None, help="CSV of permitted hardware tiers; default from AXONOS_GUEST_ALLOWED_PROFILES")
    p_mint.add_argument("--templates", default=None, help="CSV of permitted environments; default from AXONOS_GUEST_ALLOWED_TEMPLATES (empty = any)")
    p_mint.add_argument("--ttl-hours", type=int, default=None, help="how long the LINK stays redeemable; default from AXONOS_GUEST_INVITE_TTL_HOURS")
    p_mint.add_argument("--base-url", default="https://axonconsole.io", help="origin used to render the demo link")
    p_mint.add_argument("--sponsor", default=None,
                        help="attribute the invite to a team member's wallet (applies their per-sponsor quotas)")
    p_mint.set_defaults(func=cmd_mint)

    p_list = sub.add_parser("list", help="list invites and their usage")
    p_list.add_argument("--limit", type=int, default=100)
    p_list.set_defaults(func=cmd_list)

    p_revoke = sub.add_parser("revoke", help="revoke an invite and end demos started from it")
    p_revoke.add_argument("--token", default=None, help="the raw invite token")
    p_revoke.add_argument("--token-hash", default=None, help="the stored token hash (from `list`)")
    p_revoke.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    _preflight(require_enabled=args.command == "mint")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
