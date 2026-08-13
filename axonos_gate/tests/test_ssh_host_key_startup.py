import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
STARTUP = (ROOT / "startup.sh").read_text(encoding="utf-8")


class SshHostKeyStartupContractTests(unittest.TestCase):
    def test_persistent_keys_are_root_owned_and_permissioned(self) -> None:
        self.assertIn('chown root:root "$HOSTKEY_DIR" || exit 1', STARTUP)
        self.assertIn('chmod 700 "$HOSTKEY_DIR" || exit 1', STARTUP)
        self.assertIn('chown root:root "$HOSTKEY_DIR"/ssh_host_*_key', STARTUP)
        self.assertIn('chmod 600 "$HOSTKEY_DIR"/ssh_host_*_key || exit 1', STARTUP)
        self.assertIn('chmod 644 "$HOSTKEY_DIR"/ssh_host_*_key.pub || exit 1', STARTUP)

    def test_symlinked_keys_are_rejected_and_public_keys_are_derived(self) -> None:
        self.assertIn('if [ -L "$key_path" ]', STARTUP)
        self.assertIn("refusing symlinked SSH host-key path", STARTUP)
        self.assertIn('ssh-keygen -y -f "$HOSTKEY_DIR/ssh_host_ed25519_key"', STARTUP)
        self.assertIn('ssh-keygen -y -f "$HOSTKEY_DIR/ssh_host_rsa_key"', STARTUP)

    def test_fingerprint_is_strict_atomic_and_fail_closed(self) -> None:
        self.assertIn("mktemp /run/axonos/ssh-host-ed25519.sha256.XXXXXX", STARTUP)
        self.assertIn("FINGERPRINT_LINE=$(ssh-keygen -lf", STARTUP)
        self.assertIn("FINGERPRINT_VALUE=$(printf", STARTUP)
        self.assertIn("^SHA256:[A-Za-z0-9+/]{43}$", STARTUP)
        self.assertIn('mv -f "$FINGERPRINT_TMP" /run/axonos/ssh-host-ed25519.sha256 || exit 1', STARTUP)
        self.assertIn("could not derive SSH host-key fingerprint", STARTUP)
        self.assertIn("derived SSH host-key fingerprint has an invalid format", STARTUP)


if __name__ == "__main__":
    unittest.main()
