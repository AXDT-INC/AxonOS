# 🧬 AxonOS Technical User Flow & System Architecture

This document outlines the workflows and architecture of the AxonOS gateway, billing verification engine, and session orchestration layer.

---

## 🏛️ 1. High-Level System Architecture

The client communicates with the gated server using standard WebSockets and HTTPS endpoints. The gated server verifies Web3 signatures, indexes on-chain deposit logs via an Ethereum RPC node, persists credit balances in PostgreSQL, and schedules session containers dynamically using local Docker CLI or a remote HTTP launcher service.

```mermaid
graph TD
    Client["Web Browser (noVNC Theme / UI)"] <--> |Port 6080: WebSockets & HTTP| Gate["Gate Server (websockify_gate.py / gate_server.py)"]
    Gate <--> |Auth, Deposit & Audit Ledgers| DB[("PostgreSQL DB (axgt_deposits, axgt_ledger)")]
    Gate <--> |JSON-RPC (balanceOf & tx checks)| EthRPC["Ethereum RPC Node (Mainnet)"]
    Gate --> |Claim / Heartbeat / Queue| SM["Session Manager (session_manager.py)"]
    SM --> |Spawn / Stop Adapter| SL["Session Launcher (session_launcher.py)"]
    SL --> |"Launcher Mode (CLI or HTTP API)"| HostLauncher["Host Docker Launcher / CLI"]
    HostLauncher --> |Manage Lifecycle| DockerContainers["AxonOS Desktop Containers (XFCE, Assistant, VNC)"]
    DockerContainers -.-> |VNC Stream via WebSocket| Client
```

---

## 🔑 2. Web3 Authentication & Deposit-Credit Flow

Users must prove wallet ownership via an EIP-191 challenge-signature process. Once authenticated, users can retrieve a server-calculated discount quote based on their current on-chain AXGT balance. Paying the discounted ETH amount to the revenue wallet grants user minutes after transaction confirmation checks.

```mermaid
sequenceDiagram
    autonumber
    actor User as User / Wallet
    participant UI as Browser Web UI (noVNC Theme)
    participant Gate as Gate Server (gate_server.py)
    participant Eth as Ethereum RPC Node
    participant DB as Postgres DB (Deposit Ledger)

    %% Authentication
    User->>UI: Connect Wallet
    UI->>Gate: GET /api/auth/challenge?wallet_address=0x...
    Gate-->>UI: Return signed challenge message
    User->>UI: Sign challenge message (personal_sign)
    UI->>Gate: POST /api/auth/verify-wallet (signature)
    Gate->>Gate: Verify signature matches wallet address
    Gate-->>UI: Issue Auth Token (cookie/body)

    %% Deposit / Credit Verification
    UI->>Gate: GET /api/discount/quote?wallet_address=0x...
    Gate->>Eth: RPC balanceOf(wallet)
    Eth-->>Gate: Return AXGT Balance
    Gate->>Gate: Calculate Tier & Discount (Pay with ETH rate)
    Gate-->>UI: Return Discount Quote (min ETH, expected minutes)
    User->>Eth: Send discount-adjusted ETH to Revenue Wallet
    Eth-->>User: Return Transaction Hash (tx_hash)
    UI->>Gate: POST /api/auth/verify-deposit (wallet_address, tx_hash) (requires token)
    loop Every 10s (Polling)
        Gate->>Eth: Get Transaction Receipt & Block Confirmations
        Eth-->>Gate: Tx Status (confirmations count)
        Note over Gate: Need min 6 confirmations
        Gate-->>UI: Return status (pending: true / confirmations)
    end
    Gate->>Eth: Verify Sender, Recipient (Revenue Wallet), Value, and final AXGT balance
    Gate->>DB: Record verified tx, credit minutes to ledger
    Gate-->>UI: Return verified: true with remaining_minutes
```

---

## ⚡ 3. GPU Session Scheduler & Billing Heartbeat

During session creation, the client claims a resource profile (e.g. Small, Medium, Large, or Max). The scheduler assigns physical GPUs exclusively. If resources are constrained, users are queued fairly based on runnability. During active sessions, client heartbeats incrementally bill credits proportional to the profile's GPU weight.

```mermaid
sequenceDiagram
    autonumber
    actor User as User
    participant UI as Browser Web UI
    participant SM as Session Manager (session_manager.py)
    participant SL as Session Launcher (session_launcher.py)
    participant DB as Postgres DB (Deposit Ledger)
    participant Docker as Docker Engine

    %% Claim Session
    User->>UI: Claim Session (Select GPU Profile: Small/Med/Large/Max)
    UI->>SM: POST /api/session/claim (requested_profile)
    SM->>DB: Query remaining_minutes for wallet
    DB-->>SM: Return remaining_minutes
    alt remaining_minutes < required_minutes (GPUs count)
        SM-->>UI: Error: Insufficient credits
    else Credits OK
        SM->>SM: Check for available physical GPUs
        alt Free GPUs >= Profile GPUs
            SM->>SM: Assign exclusive GPU IDs
            SM->>SL: Launch Session (Profile, Container details)
            SL->>Docker: Spawn container with --gpus='"device=ID1,ID2"'
            Docker-->>SL: Container ID / Port
            SL-->>SM: Success
            SM->>DB: Update Session status (active)
            SM-->>UI: Launch details & WebSocket token
        else Not enough free GPUs
            SM->>SM: Add to Queue (FIFO, Schedulability-aware)
            SM-->>UI: Status: Queueing (Position, Reason: insufficient GPUs)
        end
    end

    %% Billing Heartbeat Loop
    loop Every 30 seconds
        UI->>SM: POST /api/session/heartbeat (session_id)
        SM->>DB: Deduct elapsed minutes * GPU weight from remaining_minutes
        DB-->>SM: Return new balance (remaining_minutes)
        alt remaining_minutes <= 0
            SM->>SL: Stop Session
            SL->>Docker: Stop and remove container
            SM->>DB: Mark session terminated / paused (credits exhausted)
            SM-->>UI: Heartbeat Response: credit exhausted (session ended)
        else Credits OK
            SM-->>UI: Heartbeat Response (Ok, remaining_minutes)
        end
    end
```
