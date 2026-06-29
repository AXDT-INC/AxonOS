import os
import sys
import base64
import json
from unittest.mock import patch, MagicMock

# Resolve paths
script_dir = os.path.dirname(os.path.abspath(__file__))
gate_dir = os.path.dirname(script_dir)
if gate_dir not in sys.path:
    sys.path.insert(0, gate_dir)

import x402_verifier as x
import x402_facilitator as fac_mod

# Deterministic test keys/addresses
_PRIVKEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
_SIGNER = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
_REVENUE = "0x1111111111111111111111111111111111111111"
_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

def run_mocked_verification():
    # Build standard dummy v2 envelope
    accepted_req = {
        "scheme": "exact",
        "network": "eip155:8453",
        "asset": _USDC,
        "amount": "1000000",
        "payTo": _REVENUE,
        "extensions": {
            "bazaar": {
                "discoverable": True,
                "category": "compute",
                "tags": ["gpu"]
            }
        }
    }
    
    # Standard EVM authorization payload details
    authorization = {
        "to": _REVENUE,
        "from": _SIGNER,
        "value": "1000000",
        "validAfter": "0",
        "validBefore": "9999999999",
        "nonce": "0x" + "11" * 32
    }
    
    # Well-formed fake signature
    sig = "0x" + "aa" * 65
    
    v2_envelope = {
        "x402Version": 2,
        "accepted": accepted_req,
        "payload": {
            "authorization": authorization,
            "signature": sig
        }
    }
    header = base64.b64encode(json.dumps(v2_envelope).encode()).decode()
    
    # Environment variables
    env_patch = patch.dict(os.environ, {
        "AXGT_X402_FACILITATOR_ENABLED": "true",
        "AXGT_X402_BAZAAR_DISCOVERABLE": "true",
        "AXGT_PUBLIC_BASE_URL": "https://app.axonos.io",
        "AXGT_REVENUE_WALLET": _REVENUE,
        "USDC_CONTRACT_ADDRESS": _USDC,
        "USDC_RPC_URL": "https://base.example.com",
        "USDC_CHAIN_ID": "8453",
        "USDC_NETWORK": "base",
        "USDC_MIN_DEPOSIT": "1",
        "X402_SETTLEMENT_PRIVATE_KEY": _PRIVKEY,
    })
    
    # Ensure facilitator module is imported so it's in sys.modules
    try:
        from axonos_gate import x402_facilitator
    except ImportError:
        import x402_facilitator
    
    mock_enabled = patch.object(fac_mod, "facilitator_enabled", return_value=True)
    mock_verify = MagicMock(return_value=(True, None, None, {}))
    mock_settle = MagicMock(return_value=("0x" + "ab" * 32, None, None, {}))
    
    # Patch module level methods on all loaded modules
    for name in ("x402_facilitator", "axonos_gate.x402_facilitator"):
        if name in sys.modules:
            mod = sys.modules[name]
            mod.facilitator_enabled = MagicMock(return_value=True)
            mod.facilitator_verify = mock_verify
            mod.facilitator_settle = mock_settle
            
    mock_recovery = patch.object(x, "_recover_eip3009_signer", return_value=_SIGNER)
    mock_verify_usdc = patch.object(x, "verify_usdc_deposit", return_value={"verified": True, "credited_minutes": 60})
    mock_wait = patch.object(x, "_wait_for_confirmations", return_value=None)
    
    print("Executing settle_x402_payment in mocked verification harness...")
    with env_patch, mock_recovery, mock_verify_usdc, mock_wait:
        result = x.settle_x402_payment(authenticated_wallet=_SIGNER, x_payment_header=header)
        print(f"\nResult: {json.dumps(result, indent=2)}")
    
    # Inspect verify and settle stubs
    if mock_verify.called:
        called_args, called_kwargs = mock_verify.call_args
        verify_payload = called_args[0]
        verify_reqs = called_args[1]
        verify_version = called_kwargs.get("x402_version") if "x402_version" in called_kwargs else called_args[2]
        
        print("\n=== INTERCEPTED CDP VERIFY PAYLOAD ===")
        print(f"Facilitator Verify Version: {verify_version}")
        print(json.dumps(verify_payload, indent=2))
        
        # Self-checks
        assert verify_payload["x402Version"] == 2
        assert isinstance(verify_payload["resource"], dict)
        assert verify_payload["resource"]["url"] == "https://app.axonos.io/api/x402/session"
        assert verify_payload["accepted"]["network"] == "eip155:8453"
        assert "info" in verify_payload["accepted"]["extensions"]["bazaar"]
        assert "schema" in verify_payload["accepted"]["extensions"]["bazaar"]
        print("\nVerification Succeeded! The payload adheres to v2 specification.")
    else:
        print("\nError: facilitator_verify was not called!")

if __name__ == "__main__":
    run_mocked_verification()
