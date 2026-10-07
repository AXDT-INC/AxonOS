"""Offline ownership bootstrap regressions for both documented agent harnesses."""

import ast
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import MagicMock

from eth_account import Account
from eth_account.messages import encode_defunct


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tools/x402-agent-test"


class AgentOwnershipBootstrapTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((HARNESS / "agent.py").read_text())
        node = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "verify_wallet")
        namespace = {"encode_defunct": encode_defunct}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<agent ownership>", "exec"), namespace)
        self.verify_wallet = namespace["verify_wallet"]
        self.account = Account.create()
        self.challenge = f"AxonOS verify\nWallet: {self.account.address.lower()}\nNonce: fixture\nIssuedAt: 123"
        self.http = MagicMock()
        self.http.get.return_value.status_code = 200
        self.http.get.return_value.json.return_value = {"challenge": self.challenge}
        self.http.post.return_value.status_code = 200
        self.http.post.return_value.json.return_value = {
            "wallet_address": self.account.address, "verified": False,
            "auth_token": "ownership-fixture",
        }

    def test_python_unfunded_wallet_bootstraps_with_real_personal_signature(self):
        for status in (200, 403):
            with self.subTest(status=status):
                self.http.post.return_value.status_code = status
                token = self.verify_wallet(self.http, "https://gate.example", self.account)
                self.assertEqual(token, "ownership-fixture")
                body = self.http.post.call_args.kwargs["json"]
                self.assertEqual(body["message"], self.challenge)
                self.assertEqual(Account.recover_message(
                    encode_defunct(text=self.challenge), signature=body["signature"]
                ), self.account.address)
                self.assertEqual(self.http.post.call_args.args[0],
                                 "https://gate.example/api/auth/verify-wallet")

    def test_python_rejects_another_wallet_challenge_before_signing(self):
        self.http.get.return_value.json.return_value = {"challenge": "AxonOS verify\nWallet: other\nNonce: fixture"}
        with self.assertRaises(RuntimeError):
            self.verify_wallet(self.http, "https://gate.example", self.account)
        self.http.post.assert_not_called()

    def test_python_rejects_missing_token_wrong_identity_and_failed_verification(self):
        cases = (
            (200, {"wallet_address": self.account.address}),
            (200, {"wallet_address": "0x" + "f" * 40, "auth_token": "fixture"}),
            (401, {"wallet_address": self.account.address, "auth_token": "fixture"}),
            (503, {"wallet_address": self.account.address, "auth_token": "fixture"}),
        )
        for status, result in cases:
            with self.subTest(status=status, result=result):
                self.http.post.return_value.status_code = status
                self.http.post.return_value.json.return_value = result
                with self.assertRaises(RuntimeError):
                    self.verify_wallet(self.http, "https://gate.example", self.account)

    def test_javascript_bootstrap_accepts_unfunded_wallet_and_rejects_invalid_proof(self):
        node = shutil.which("node")
        if not node:
            try:
                import playwright
                candidate = Path(playwright.__file__).resolve().parent / "driver/node"
                node = str(candidate) if candidate.exists() else None
            except ImportError:
                pass
        if not node:
            self.skipTest("Node runtime unavailable")
        source = (HARNESS / "agent.mjs").read_text()
        helper = "async function verifyWallet" + source.split("async function verifyWallet", 1)[1].split("// --- 0.", 1)[0]
        program = "const assert = require('node:assert/strict');\n" + helper + r"""
(async () => {
  const address = '0x' + 'a'.repeat(40);
  const challenge = `AxonOS verify\nWallet: ${address}\nNonce: fixture\nIssuedAt: 123`.replaceAll('\\n', '\n');
  let signed = 0;
  const account = {address, signMessage: async ({message}) => {
    assert.equal(message, challenge); signed++; return 'signature-fixture';
  }};
  for (const status of [200, 403]) {
    const calls = [];
    const request = async (url, options) => {
      calls.push([url, options]);
      return calls.length === 1
        ? {status: 200, json: async () => ({challenge})}
        : {status, json: async () => ({wallet_address: address, verified: false, auth_token: 'ownership-fixture'})};
    };
    assert.equal(await verifyWallet('https://gate.example', account, request), 'ownership-fixture');
    assert.equal(calls[0][0].searchParams.get('wallet_address'), address);
    assert.equal(calls[1][0], 'https://gate.example/api/auth/verify-wallet');
    assert.deepEqual(JSON.parse(calls[1][1].body), {wallet_address: address, message: challenge, signature: 'signature-fixture'});
  }
  assert.equal(signed, 2);
  await assert.rejects(verifyWallet('https://gate.example', account,
    async () => ({status: 200, json: async () => ({challenge: 'malicious unrelated message'})})));
  assert.equal(signed, 2);
  for (const [status, result] of [
    [200, {wallet_address: address}],
    [200, {wallet_address: 'other', auth_token: 'fixture'}],
    [401, {wallet_address: address, auth_token: 'fixture'}],
    [503, {wallet_address: address, auth_token: 'fixture'}],
  ]) {
    let count = 0;
    await assert.rejects(verifyWallet('https://gate.example', account, async () => {
      count++;
      return count === 1 ? {status: 200, json: async () => ({challenge})}
        : {status, json: async () => result};
    }));
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
"""
        result = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
