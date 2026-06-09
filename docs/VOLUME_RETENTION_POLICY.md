# Persistent Storage Volume Retention & Cleanup Policy

AxonOS provides users with persistent storage for their desktop sessions backed by Docker named volumes (`axgt-user-storage-<sanitized_wallet>`).

AxonOS bills users for storage disk usage when they are offline, allows balances to go negative (representing debt), and prunes their volumes once they exceed their debt threshold limit.

---

## 1. Lifecycle Summary

| Lifecycle Event | Container State | Volume State | Action / Billing |
| :--- | :--- | :--- | :--- |
| **Active Session** | Running | Mounted | Deducting prepaid credits from user balance. |
| **Credits Exhausted** | Running (Grace Period) | Mounted | Active for **2 hours** to allow top-up. Billing stopped. |
| **Grace Period Expiry** | **Destroyed** | Unmounted (Saved) | Resources (GPU, CPU, RAM) released. Volume saved. |
| **Offline Storage Billing** | N/A | Saved | Storage charges accrue to balance. Credit balance goes negative. |
| **Debt Limit Exceeded** | N/A | **Pruned/Deleted** | Volume is deleted when balance drops below the negative debt threshold. |

---

## 2. Configured Variables

You can configure this automatic pruning behavior inside your `.env` file:

- `AXGT_PERSISTENT_STORAGE_GB_HOUR_COST_MINUTES`: The storage cost in equivalent desktop minutes per GB per hour (defaults to `0.05` minutes/GB-hour).
- `AXGT_PERSISTENT_STORAGE_CLEANUP_INTERVAL_SECONDS`: How often the background thread sweeps the database to apply storage billing and check for expired volumes (default: `3600` / 1 hour).
- `AXGT_PERSISTENT_STORAGE_MIN_BALANCE_LIMIT_MINUTES`: The maximum storage debt allowed before volume deletion, expressed as a negative value (default: `-1440.0` minutes / -24 hours of standard compute equivalent).

---

## 3. Built-in Stack Automation (Recommended)

The AxonOS stack manages volume cleanup automatically inside the `axonos-launcher` service container. A daemon thread runs in the background of the launcher service, calculates volume sizes, applies storage deductions, and prunes user volumes that exceed the debt limit.

---

## 4. Using the Pruning Script

The host-side volume pruning utility is located at `scripts/prune_user_volumes.py`. It allows administrators to manually check and prune volumes exceeding the debt limit.

### Command Arguments

- `--debt-limit <float>`: Set a custom debt threshold limit in minutes (default: reads `AXGT_PERSISTENT_STORAGE_MIN_BALANCE_LIMIT_MINUTES`).
- `--prefix <string>`: Set the prefix of the named volumes (default: `axgt-user-storage-`).
- `--dry-run`: Performs database queries and checks volume existence without deleting anything.

### Running a Dry Run
```bash
# Set database URL (matches compose DB URL)
export AXGT_CHALLENGE_DB_URL="postgresql://axonos_gate:axonos_gate_secret@localhost:5432/axonos_gate"

# Run dry run for custom debt limit of -500 minutes
python3 scripts/prune_user_volumes.py --debt-limit -500.0 --dry-run
```

### Running the Active Prune
```bash
python3 scripts/prune_user_volumes.py
```
