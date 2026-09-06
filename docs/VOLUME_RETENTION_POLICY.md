# Persistent Storage Volume Retention & Cleanup Policy

AxonOS provides users with persistent storage for their desktop sessions backed by loop-backed `ext4` Docker volumes (`axgt-user-storage-<sanitized_wallet>`). Volumes are provisioned as sparse `.ext4` image files under `/var/lib/docker/axonos_storage/`.

Users select their desired storage capacity (10 GB – 500 GB, default 100 GB) via the launch wizard slider. Volumes automatically scale up online when a user requests a larger size (`truncate` + `losetup -c` + `resize2fs`) without downtime or data migration; capacity is growth-only. Storage billing is calculated based on **actual files stored** (`du -s`, measured by a throwaway `alpine` container mounting the volume — the `alpine` image must be available on the host; a failed measurement bills nothing for that sweep), so allocating larger virtual capacity does not over-charge the user or consume physical host space for empty unwritten blocks.

AxonOS bills every retained volume for its storage footprint, allows balances to go negative (representing debt), and prunes volumes once they exceed the debt threshold limit. The ledger labels these rows "Offline storage charge", but the sweep does **not** exclude wallets with a running session: storage is charged continuously for as long as the volume exists, in addition to compute billing while a session is active. Treat "offline" in the ledger text as legacy wording.

Demo/guest sessions and launches flagged `ephemeral_storage` receive no persistent volume and are outside this policy. The whole subsystem is gated by `AXGT_PERSISTENT_STORAGE_ENABLED` (default `true`).

---

## 1. Lifecycle Summary

| Lifecycle Event | Container State | Volume State | Action / Billing |
| :--- | :--- | :--- | :--- |
| **Active Session** | Running | Mounted | Compute credits deducted on heartbeat; the storage sweep also charges the volume footprint. |
| **Credits Exhausted** | Running (Grace Period) | Mounted | Active for **2 hours** to allow top-up. Compute billing stopped; storage billing continues under this policy. |
| **Grace Period Expiry** | **Destroyed** | Unmounted (Saved) | Resources (GPU, CPU, RAM) released. Volume saved. |
| **Storage Billing (no session)** | N/A | Saved | Storage charges keep accruing to the balance. Credit balance may go negative. |
| **Debt Limit Exceeded** | N/A | **Pruned/Deleted** | Volume is deleted when balance drops below the negative debt threshold. |

---

## 2. Configured Variables

You can configure this automatic pruning behavior inside your `.env` file:

- `AXGT_PERSISTENT_STORAGE_ENABLED`: Master switch for per-wallet volumes and the billing/prune sweep (default: `true`).
- `AXGT_PERSISTENT_STORAGE_DIR`: Host directory for the sparse `.ext4` images (default: `/var/lib/docker/axonos_storage`; must match the launcher bind mount in `docker-compose.yml`).
- `AXGT_PERSISTENT_STORAGE_MOUNT_PATH`: In-container mount point (default: `/home/aXonian`).
- `AXGT_PERSISTENT_STORAGE_VOLUME_PREFIX`: Docker volume name prefix (default: `axgt-user-storage-`).
- `AXGT_SESSION_CREDIT_GRACE_MINUTES`: How long a credit-exhausted running container is retained for top-up before teardown (default: `120` minutes).
- `AXGT_PERSISTENT_STORAGE_GB_HOUR_COST_MINUTES`: The storage cost in equivalent desktop minutes per GB per hour (defaults to `0.05` minutes/GB-hour).
- `AXGT_PERSISTENT_STORAGE_CLEANUP_INTERVAL_SECONDS`: How often the background thread sweeps volumes to apply storage billing and prune over-drawn wallets (default: `3600` / 1 hour; minimum `60`). The first sweep runs 30 s after the launcher starts. Charges are **elapsed-time based**, not per-sweep: each wallet is billed from its last storage ledger row (or the volume's creation time, whichever is later), a sweep less than 60 s after the previous charge is skipped, and a single charge never covers more than 168 h.
- `AXGT_PERSISTENT_STORAGE_MIN_BALANCE_LIMIT_MINUTES`: The maximum storage debt allowed before volume deletion, expressed as a negative value (default: `-1440.0` minutes / -24 hours of standard compute equivalent).

See [`ENVIRONMENT_VARIABLES.md`](ENVIRONMENT_VARIABLES.md#persistent-storage-per-wallet-volumes) for the full table.

---

## 3. Built-in Stack Automation (Recommended)

The AxonOS stack manages volume cleanup automatically inside the `axonos-launcher` service container. A daemon thread runs in the background of the launcher service (only while `AXGT_PERSISTENT_STORAGE_ENABLED` is truthy), lists volumes matching the prefix, measures each with `du -s`, writes a `usage_deduction` ledger row per charge (`created_by = volume_billing_daemon`), and prunes user volumes whose balance is below the debt limit — both before charging and immediately after a charge pushes a wallet under the limit. A volume whose sanitized wallet has no `axgt_deposits` row is skipped.

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
