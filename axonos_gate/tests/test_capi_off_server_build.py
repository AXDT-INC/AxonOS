"""Source/recipe tests only: no Docker invocation or real credential access."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import io

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('off_server', ROOT / 'scripts/build_capi_off_server.py')
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


class OffServerBuildTests(unittest.TestCase):
    def test_required_mounts_and_no_password_args(self):
        source = (ROOT / 'Dockerfile').read_text()
        self.assertEqual(source.count('--mount=type=secret,id=axonos_password,required=true'), 2)
        self.assertEqual(source.count('test -s /run/secrets/axonos_password'), 2)
        self.assertNotIn('ARG PASSWORD', source)
        self.assertNotIn('$PASSWORD', source)
        self.assertNotIn('PASSWORD: ${AXONOS_VNC_PASSWORD}', (ROOT / 'docker-compose.yml').read_text())

    def test_authentication_inputs_and_missing_empty_refusal(self):
        source = (ROOT / 'Dockerfile').read_text()
        account = next(line.strip().split(' | chpasswd')[0] for line in source.splitlines() if '| chpasswd' in line)
        vnc = next(line.strip().split(' | vncpasswd')[0] for line in source.splitlines() if '| vncpasswd' in line)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            secret = Path(directory) / 'synthetic'
            for content in (b'Synthetic$!\\literal', b''):
                secret.write_bytes(content)
                for expression, expected in ((account, b'aXonian:' + content + b'\n'), (vnc, content + b'\n')):
                    # Test only the extracted stdin producer; never run useradd,
                    # chpasswd, vncpasswd, chmod or any account mutation.
                    command = 'test -s "$TEST_SECRET" && ' + expression.replace('/run/secrets/axonos_password', '"$TEST_SECRET"')
                    result = subprocess.run(['/bin/sh', '-c', command], env={**os.environ, 'USER':'aXonian', 'TEST_SECRET':str(secret)}, capture_output=True)
                    if content:
                        self.assertEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, expected)
                    else:
                        self.assertNotEqual(result.returncode, 0)
            secret.unlink()
            result = subprocess.run(['/bin/sh', '-c', 'test -s "$TEST_SECRET"'], env={'TEST_SECRET':str(secret)}, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def _recipe_arguments(self, prefix_cache=False):
        calls = []
        revision = 'a' * 40
        def fake(args, **kwargs):
            calls.append(args)
            if args[:3] == ['git','rev-parse','HEAD']: return revision + '\n'
            if args[:3] == ['docker','image','inspect']: return 'sha256:' + 'b' * 64
            return ''
        # Keep fixture outside ROOT without accessing any real secret.
        with patch.dict(os.environ, {'AXONOS_OFF_SERVER_BUILD_APPROVED':'yes', 'AXONOS_BUILD_REVISION':revision,
             'AXONOS_BUILD_BUILDER':'isolated', 'AXONOS_GATE_BASE':'nvidia/cuda@sha256:'+'c'*64,
             'NVIDIA_DRIVER_PKG_VERSION':'synthetic-version', 'AXONOS_BUILD_PASSWORD_FILE':'/nonexistent/synthetic',
             'AXONOS_SYNTHETIC_PREFIX_CACHE':'yes' if prefix_cache else ''}, clear=True), \
             patch.object(BUILD.socket,'gethostname',return_value='offserver'), \
             patch.object(BUILD,'run',side_effect=fake), \
             patch.object(Path,'is_file',return_value=True), patch.object(Path,'is_symlink',return_value=False), \
             patch.object(Path,'stat') as stat:
            stat.return_value.st_mode = 0o100600
            original = Path.read_bytes
            def read(path):
                return b'Synthetic-only' if str(path)=='/nonexistent/synthetic' else original(path)
            with patch.object(Path,'read_bytes',read), contextlib.redirect_stdout(io.StringIO()) as output:
                BUILD.main()
            self.assertNotIn('Synthetic-only', output.getvalue())
        builds = [args for args in calls if args[:3]==['docker','buildx','build']]
        self.assertEqual(len(builds),2)
        for index, args in enumerate(builds):
            self.assertEqual('--no-cache' in args, not prefix_cache or index == 1)
            self.assertIn('--load',args)
            self.assertNotIn('--push',args)
            self.assertFalse(any('PASSWORD=' in arg for arg in args))
            self.assertNotIn('latest', ' '.join(args))
        self.assertIn('--secret',builds[0])
        self.assertNotIn('--secret',builds[1])
        self.assertIn('docker/x-capi-worker/Dockerfile',builds[1])
        return builds

    def test_recipe_arguments_and_outputs_with_mocked_builder(self):
        self._recipe_arguments()

    def test_prefix_cache_uses_fresh_nonsecret_nonce_and_worker_stays_no_cache(self):
        first = self._recipe_arguments(prefix_cache=True)
        second = self._recipe_arguments(prefix_cache=True)
        def nonce(builds):
            values = [arg.split('=',1)[1] for arg in builds[0]
                      if arg.startswith('AXONOS_CREDENTIAL_CACHE_NONCE=')]
            self.assertEqual(len(values),1)
            self.assertRegex(values[0], r'^[0-9a-f]{64}$')
            return values[0]
        self.assertNotEqual(nonce(first), nonce(second))

    def test_both_secret_steps_require_nonce_before_secret_access(self):
        source = (ROOT/'Dockerfile').read_text()
        self.assertEqual(source.count('ARG AXONOS_CREDENTIAL_CACHE_NONCE'),1)
        for step in source.split('RUN --mount=type=secret,id=axonos_password,required=true')[1:]:
            self.assertLess(step.index('${AXONOS_CREDENTIAL_CACHE_NONCE:?fresh build nonce required}'),
                            step.index('test -s /run/secrets/axonos_password'))
        guard='test -n "${AXONOS_CREDENTIAL_CACHE_NONCE:?fresh build nonce required}"'
        for env, allowed in [({},False),({'AXONOS_CREDENTIAL_CACHE_NONCE':''},False),
                             ({'AXONOS_CREDENTIAL_CACHE_NONCE':'a'*64},True)]:
            result=subprocess.run(['/bin/sh','-c',guard],env=env,capture_output=True)
            self.assertEqual(result.returncode==0,allowed)

    def test_kubo_archive_is_checked_before_extraction(self):
        source = (ROOT / 'Dockerfile').read_text()
        step = source.split('# Install IPFS CLI', 1)[1].split('\nRUN ', 2)[1]
        self.assertIn('https://github.com/ipfs/kubo/releases/download/v0.24.0/kubo_v0.24.0_linux-amd64.tar.gz', step)
        self.assertIn("--proto '=https' --proto-redir '=https'", step)
        self.assertIn('--connect-timeout 20 --max-time 600 --retry 0', step)
        self.assertIn('80aadb72fa23b216310dc0c9f307ba383ab90fbb43043ab321d9f0f8edc74b8c28bb9c2ef73b702814aba379bd15b786d9ca4330474a3f807a83211db3f0e279', step)
        self.assertIn('sha512sum -c - &&', step)
        self.assertLess(step.index('sha512sum -c -'), step.index('tar -xzf'))
        self.assertLess(step.index('tar -xzf'), step.index('bash install.sh'))

    def test_production_host_refuses_before_commands(self):
        with patch.object(BUILD.socket,'gethostname',return_value='clu1'), patch.object(BUILD,'run') as run:
            with self.assertRaises(RuntimeError): BUILD.main()
            run.assert_not_called()

    def test_frozen_contract(self):
        directory=ROOT/'contracts/x-capi-handoff/v2'
        manifest=directory/'manifest.json'
        self.assertEqual(hashlib.sha256(manifest.read_bytes()).hexdigest(),'085953bf44b097d571c99aff4dc18ff92fe539b3cddec6bf2727f2f56ab447d8')
        for name,digest in json.loads(manifest.read_text())['files'].items():
            self.assertEqual(hashlib.sha256((directory/name).read_bytes()).hexdigest(),digest)

if __name__=='__main__': unittest.main()
