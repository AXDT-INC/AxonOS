#!/usr/bin/env python3
"""
Volume cleanup utility for AxonOS hosts.
Finds users whose credit balance has fallen below the negative debt threshold,
and prunes their persistent Docker storage volumes to reclaim disk space.
"""

import argparse
import os
import sys
import time
import subprocess

try:
    import psycopg2
except ImportError:
    print("Error: 'psycopg2' package is required to connect to the database.", file=sys.stderr)
    print("Please install it on the host (e.g., pip install psycopg2-binary).", file=sys.stderr)
    sys.exit(1)


def sanitize_wallet(wallet: str) -> str:
    return "".join(c for c in wallet if c.isalnum() or c in ("-", "_")).lower()


def get_debt_limit_exceeded_wallets(db_url: str, min_balance_limit: float):
    try:
        conn = psycopg2.connect(db_url)
        with conn.cursor() as cur:
            # Find wallets whose remaining_minutes has fallen below the debt threshold
            cur.execute(
                """
                SELECT wallet_address, remaining_minutes, updated_at
                FROM axgt_deposits
                WHERE remaining_minutes < %s
                """,
                (min_balance_limit,)
            )
            rows = cur.fetchall()
            return [{"wallet": r[0], "remaining": r[1], "updated_at": r[2]} for r in rows]
    except Exception as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return []
    finally:
        if 'conn' in locals() and conn:
            conn.close()


def prune_volume(volume_name: str, dry_run: bool = True) -> bool:
    # Check if volume exists
    check_cmd = ["docker", "volume", "inspect", volume_name]
    res = subprocess.run(check_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        # Volume doesn't exist on this host, skip
        return False

    # Measure volume size
    size_kb = 0.0
    size_cmd = ["docker", "run", "--rm", "-v", f"{volume_name}:/volume-data", "alpine", "du", "-s", "/volume-data"]
    try:
        out = subprocess.check_output(size_cmd, stderr=subprocess.STDOUT, text=True, timeout=15).strip()
        parts = out.split()
        if parts:
            size_kb = float(parts[0])
    except Exception:
        pass
    size_gb = size_kb / (1024.0 * 1024.0)

    if dry_run:
        print(f"[DRY-RUN] Would remove docker volume: {volume_name} (Size: {size_gb:.4f} GB)")
        return True
    
    print(f"Removing docker volume: {volume_name} (Size: {size_gb:.4f} GB)...")
    rm_cmd = ["docker", "volume", "rm", volume_name]
    rm_res = subprocess.run(rm_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if rm_res.returncode == 0:
        print(f"Successfully removed volume: {volume_name}")
        return True
    else:
        print(f"Error removing volume {volume_name}: {rm_res.stderr.strip()}", file=sys.stderr)
        return False


def main():
    default_limit_env = os.getenv("AXGT_PERSISTENT_STORAGE_MIN_BALANCE_LIMIT_MINUTES")
    try:
        default_limit = float(default_limit_env) if default_limit_env else -1440.0
    except ValueError:
        default_limit = -1440.0

    parser = argparse.ArgumentParser(description="Prune persistent storage volumes for AxonOS wallets exceeding debt limit.")
    parser.add_argument("--debt-limit", type=float, default=default_limit, help=f"Debt threshold limit in minutes (default: {default_limit})")
    parser.add_argument("--prefix", type=str, default="axgt-user-storage-", help="Docker volume prefix (default: axgt-user-storage-)")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without deleting volumes")
    
    args = parser.parse_args()
    
    db_url = os.getenv("AXGT_CHALLENGE_DB_URL")
    if not db_url:
        print("Error: AXGT_CHALLENGE_DB_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)
        
    print(f"Querying database for wallets with balance below debt limit ({args.debt_limit} minutes)...")
    
    expired_users = get_debt_limit_exceeded_wallets(db_url, args.debt_limit)
    if not expired_users:
        print("No wallets exceeding debt limit criteria were found.")
        return
        
    print(f"Found {len(expired_users)} wallets exceeding debt limit. Processing volumes...")
    pruned_count = 0
    for user in expired_users:
        wallet = user["wallet"]
        safe_wallet = sanitize_wallet(wallet)
        volume_name = f"{args.prefix}{safe_wallet}"
        
        last_active = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(user["updated_at"]))
        print(f"Wallet: {wallet} (Remaining: {user['remaining']:.2f} mins, Last Update: {last_active})")
        
        if prune_volume(volume_name, dry_run=args.dry_run):
            pruned_count += 1
            
    if args.dry_run:
        print(f"[DRY-RUN] Pruning check complete. Checked {len(expired_users)} wallets, would have pruned {pruned_count} volumes.")
    else:
        print(f"Pruning complete. Successfully pruned {pruned_count} volumes.")


if __name__ == "__main__":
    main()
