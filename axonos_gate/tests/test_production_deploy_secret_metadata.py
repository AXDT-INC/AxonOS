"""Secret metadata tests: fake syscalls, or an explicitly selected root chroot.

The ordinary suite never examines /etc/axonos. The root-only fixture creates a
synthetic tree beneath this checkout and chroots its children before they use
the helper's fixed absolute paths. It never invokes sudo or installs anything.
"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import posixpath
import select
import signal
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / 'scripts/deploy_production_secret_metadata.py'
MARKER = 'SYNTHETIC_SECRET_MUST_NEVER_BE_EMITTED'
FIELDS = {'dev', 'ino', 'uid', 'gid', 'mode', 'nlink', 'size', 'mtime_ns', 'ctime_ns'}
FILES = {
    '/etc/axonos/x-capi-context-key': (10001, 44, 4096),
    '/etc/axonos/x-capi-db-url': (10001, 1, 8192),
    '/etc/axonos/x-capi-hash-key': (10001, 32, 256),
    '/etc/axonos/x-capi-postgres-bootstrap-password': (0, 16, 8192),
    '/etc/axonos/x-capi-postgres-worker-password': (0, 16, 8192),
}
DIRECTORIES = ('/', '/etc', '/etc/axonos')


def load_helper():
    spec = importlib.util.spec_from_file_location('secret_metadata_under_test', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_checks(helper):
    spec = importlib.util.spec_from_file_location('metadata_client_under_test',
                                                REPO / 'scripts/deploy_production_checks.py')
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {'deploy_production_secret_metadata': helper}):
        spec.loader.exec_module(module)
    return module


def record(inode, uid=0, mode=stat.S_IFREG | 0o600, size=64):
    return dict(dev=7, ino=inode, uid=uid, gid=uid, mode=mode, nlink=1,
                size=size, mtime_ns=1234567890, ctime_ns=1234567891)


class MetadataFilesystem:
    """Model named entries separately from already opened file descriptors."""

    def __init__(self, case):
        self.case = case
        self.entries = {
            path: record(index, mode=stat.S_IFDIR | (0o700 if path == '/etc/axonos' else 0o755))
            for index, path in enumerate(DIRECTORIES, 1)
        }
        self.entries.update({path: record(index, uid=uid)
                             for index, (path, (uid, _, _)) in enumerate(FILES.items(), 10)})
        self.descriptors = {}
        self.opened = []
        self.closed = []
        self.seen_files = set()
        self.after_initial_snapshot = None
        self.after_second_snapshot = None
        self.fstat_calls = 0
        self.triggered = False

    def path(self, path, dir_fd=None):
        path = os.fspath(path)
        if not path.startswith('/'):
            self.case.assertIn(dir_fd, self.descriptors, 'relative opens need an anchored parent')
            path = posixpath.join(self.descriptors[dir_fd][0], path)
        normalized = posixpath.normpath(path)
        self.case.assertEqual(path, normalized, 'paths must not contain traversal')
        self.case.assertIn(path, (*DIRECTORIES, *FILES), 'helper accessed an unreviewed path')
        return path

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        path = self.path(path, dir_fd)
        self.case.assertTrue(flags & os.O_PATH, 'secret must never be opened for reading')
        self.case.assertTrue(flags & os.O_NOFOLLOW, 'opens must not follow symlinks')
        self.case.assertFalse(flags & (os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_RDWR))
        if path in DIRECTORIES:
            self.case.assertTrue(flags & os.O_DIRECTORY)
        if path != '/':
            self.case.assertIsNotNone(dir_fd, 'all descendants must be opened relative to an anchor')
        if path not in self.entries:
            raise FileNotFoundError(errno.ENOENT, MARKER)
        value = self.entries[path]
        if flags & os.O_DIRECTORY and not stat.S_ISDIR(value['mode']):
            raise NotADirectoryError(errno.ENOTDIR, MARKER)
        descriptor = 100 + len(self.opened)
        self.descriptors[descriptor] = (path, copy.deepcopy(value))
        self.opened.append(descriptor)
        return descriptor

    def fstat(self, descriptor):
        self.fstat_calls += 1
        path, value = self.descriptors[descriptor]
        result = types.SimpleNamespace(**{'st_' + key: item for key, item in value.items()})
        if path in FILES:
            self.seen_files.add(path)
        if self.seen_files == set(FILES) and not self.triggered:
            self.triggered = True
            if self.after_initial_snapshot:
                self.after_initial_snapshot(self)
        if self.fstat_calls == 2 * (len(DIRECTORIES) + len(FILES)) and self.after_second_snapshot:
            self.after_second_snapshot(self)
        return result

    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        self.case.assertFalse(follow_symlinks, 'path rechecks must not follow symlinks')
        path = self.path(path, dir_fd)
        if path not in self.entries:
            raise FileNotFoundError(errno.ENOENT, MARKER)
        return types.SimpleNamespace(**{'st_' + key: item for key, item in self.entries[path].items()})

    def close(self, descriptor):
        self.case.assertIn(descriptor, self.descriptors)
        self.closed.append(descriptor)
        del self.descriptors[descriptor]

    def mutate(self, path, field, value, *, replace=False):
        self.entries[path][field] = value
        if not replace:
            for opened_path, opened_record in self.descriptors.values():
                if opened_path == path:
                    opened_record[field] = value

    def patched(self, helper):
        stack = ExitStack()
        for method in ('open', 'fstat', 'stat', 'close'):
            stack.enter_context(mock.patch.object(helper.os, method, side_effect=getattr(self, method)))
        stack.enter_context(mock.patch.object(helper.os, 'geteuid', return_value=0))
        for method in ('read', 'fdopen'):
            stack.enter_context(mock.patch.object(helper.os, method, side_effect=AssertionError('content read')))
        for method in ('read_bytes', 'read_text', 'open', 'resolve', 'lstat'):
            stack.enter_context(mock.patch.object(Path, method, side_effect=AssertionError('path/content API')))
        stack.enter_context(mock.patch('builtins.open', side_effect=AssertionError('content open')))
        stack.enter_context(mock.patch('io.open', side_effect=AssertionError('content open')))
        return stack


class SecretMetadataHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = load_helper()

    def collect(self, filesystem):
        with filesystem.patched(self.helper):
            return self.helper.collect_snapshot()

    def assert_refusal(self, filesystem):
        with self.assertRaises((self.helper.MetadataRefusal, OSError)):
            self.collect(filesystem)
        self.assertCountEqual(filesystem.opened, filesystem.closed)

    def test_snapshot_is_only_exact_integer_metadata_for_fixed_paths(self):
        filesystem = MetadataFilesystem(self)
        snapshot = self.collect(filesystem)
        self.assertEqual(set(snapshot), {'version', 'directories', 'files'})
        self.assertEqual(snapshot['version'], 1)
        self.assertEqual(set(snapshot['directories']), set(DIRECTORIES))
        self.assertEqual(set(snapshot['files']), set(FILES))
        for group in ('directories', 'files'):
            for path, value in snapshot[group].items():
                self.assertEqual(set(value), FIELDS)
                self.assertTrue(all(type(item) is int for item in value.values()))
                self.assertEqual(value, filesystem.entries[path])
        self.assertNotIn(MARKER, json.dumps(snapshot))
        self.assertCountEqual(filesystem.opened, filesystem.closed)

    def test_every_file_rejects_type_owner_group_permissions_link_count_and_size(self):
        for path, (uid, low, high) in FILES.items():
            changes = (
                ('mode', stat.S_IFLNK | 0o600), ('mode', stat.S_IFDIR | 0o600),
                ('mode', stat.S_IFIFO | 0o600), ('mode', stat.S_IFREG | 0o644),
                ('mode', stat.S_IFREG | 0o660), ('mode', stat.S_IFREG | 0o4600),
                ('uid', uid + 1), ('gid', uid + 1), ('nlink', 2),
                ('size', low - 1), ('size', high + 1),
            )
            for field, value in changes:
                with self.subTest(path=path, field=field, value=value):
                    filesystem = MetadataFilesystem(self)
                    filesystem.entries[path][field] = value
                    self.assert_refusal(filesystem)

    def test_each_file_accepts_only_reviewed_permission_and_size_boundaries(self):
        for path, (_, low, high) in FILES.items():
            for mode in (0o400, 0o600):
                for size in (low, high):
                    with self.subTest(path=path, mode=mode, size=size):
                        filesystem = MetadataFilesystem(self)
                        filesystem.entries[path].update(mode=stat.S_IFREG | mode, size=size)
                        self.assertEqual(self.collect(filesystem)['files'][path]['size'], size)

    def test_missing_file_and_untrusted_parent_fail_closed(self):
        for path in (*DIRECTORIES, *FILES):
            with self.subTest(missing=path):
                filesystem = MetadataFilesystem(self)
                del filesystem.entries[path]
                self.assert_refusal(filesystem)
        for path in DIRECTORIES:
            changes = [('uid', 1000), ('gid', 1000), ('mode', stat.S_IFLNK | 0o755),
                       ('mode', stat.S_IFDIR | 0o775), ('mode', stat.S_IFDIR | 0o757)]
            if path == '/etc/axonos':
                changes.append(('mode', stat.S_IFDIR | 0o750))
            for field, value in changes:
                with self.subTest(parent=path, field=field, value=value):
                    filesystem = MetadataFilesystem(self)
                    filesystem.entries[path][field] = value
                    self.assert_refusal(filesystem)

    def test_rechecks_parent_and_leaf_substitution_before_returning(self):
        for path in (*DIRECTORIES, *FILES):
            with self.subTest(path=path):
                filesystem = MetadataFilesystem(self)
                filesystem.after_initial_snapshot = lambda fs, path=path: fs.mutate(
                    path, 'ino', 99999, replace=True)
                self.assert_refusal(filesystem)
                self.assertTrue(filesystem.triggered)

    def test_rechecks_open_descriptor_fingerprint_before_returning(self):
        for path in FILES:
            for field, value in (('size', 65), ('mtime_ns', 987654321), ('ctime_ns', 987654322)):
                with self.subTest(path=path, field=field):
                    filesystem = MetadataFilesystem(self)
                    filesystem.after_initial_snapshot = lambda fs, path=path, field=field, value=value: fs.mutate(
                        path, field, value)
                    self.assert_refusal(filesystem)

    def test_final_fstat_detects_change_after_both_named_walks(self):
        for path in (*DIRECTORIES, *FILES):
            with self.subTest(path=path):
                filesystem = MetadataFilesystem(self)
                filesystem.after_second_snapshot = lambda fs, path=path: fs.mutate(
                    path, 'ctime_ns', 987654322)
                self.assert_refusal(filesystem)

    def test_distinct_secret_roles_cannot_alias_one_inode(self):
        filesystem = MetadataFilesystem(self)
        first, second = tuple(FILES)[:2]
        filesystem.entries[second]['ino'] = filesystem.entries[first]['ino']
        self.assert_refusal(filesystem)

    def run_main(self, filesystem, arguments=(), euid=0, *, isolated=True, no_site=True,
                 no_bytecode=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        deadline = mock.Mock()
        with filesystem.patched(self.helper), \
                mock.patch.object(self.helper.os, 'geteuid', return_value=euid), \
                mock.patch.object(self.helper.signal, 'signal', deadline.signal), \
                mock.patch.object(self.helper.signal, 'pthread_sigmask', deadline.pthread_sigmask), \
                mock.patch.object(self.helper.signal, 'alarm', deadline.alarm), \
                mock.patch.object(sys, 'flags', types.SimpleNamespace(isolated=isolated, no_site=no_site)), \
                mock.patch.object(sys, 'dont_write_bytecode', no_bytecode), \
                mock.patch.object(sys, 'argv', [str(HELPER), *arguments]), \
                mock.patch.object(sys, 'stdin', mock.Mock(read=mock.Mock(side_effect=AssertionError('stdin read')))), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = self.helper.main()
        if status == 0:
            self.assertEqual(deadline.mock_calls, [
                mock.call.signal(signal.SIGALRM, signal.SIG_DFL),
                mock.call.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM}),
                mock.call.alarm(5), mock.call.alarm(0)])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_main_root_requirement_and_no_arguments(self):
        for arguments, euid in (((), 1000), (('/etc/shadow',), 0), (('--help',), 0)):
            with self.subTest(arguments=arguments, euid=euid):
                filesystem = MetadataFilesystem(self)
                status, stdout, stderr = self.run_main(filesystem, arguments, euid)
                self.assertNotEqual(status, 0)
                self.assertEqual(stdout, '')
                self.assertTrue(stderr)
                self.assertNotIn(MARKER, stderr)
                self.assertEqual(filesystem.opened, [])

    def test_main_refuses_unsafe_interpreter_startup(self):
        for flag in ('isolated', 'no_site', 'no_bytecode'):
            with self.subTest(flag=flag):
                filesystem = MetadataFilesystem(self)
                status, stdout, stderr = self.run_main(filesystem, **{flag: False})
                self.assertNotEqual(status, 0)
                self.assertEqual(stdout, '')
                self.assertTrue(stderr)
                self.assertEqual(filesystem.opened, [])

    def test_main_ignores_environment_paths_and_emits_only_metadata(self):
        filesystem = MetadataFilesystem(self)
        with mock.patch.dict(os.environ, {'AXONOS_SECRET_DIR': '/etc/shadow',
                                         'X_CAPI_CONTEXT_KEY_FILE': '/etc/shadow'}):
            status, stdout, stderr = self.run_main(filesystem)
        self.assertEqual(status, 0)
        self.assertEqual(stderr, '')
        self.assertEqual(set(json.loads(stdout)['files']), set(FILES))
        self.assertNotIn(MARKER, stdout)

    def test_main_withholds_exception_detail_and_partial_snapshot(self):
        filesystem = MetadataFilesystem(self)
        del filesystem.entries[next(iter(FILES))]
        status, stdout, stderr = self.run_main(filesystem)
        self.assertNotEqual(status, 0)
        self.assertEqual(stdout, '')
        self.assertTrue(stderr)
        self.assertNotIn(MARKER, stderr)
        self.assertCountEqual(filesystem.opened, filesystem.closed)


class SecretMetadataClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = load_helper()
        cls.checks = load_checks(cls.helper)

    def snapshot(self):
        filesystem = MetadataFilesystem(self)
        with filesystem.patched(self.helper):
            return self.helper.collect_snapshot()

    def test_parser_requires_complete_unique_bounded_integer_schema(self):
        valid = self.snapshot()
        self.assertEqual(self.checks.parse_secret_snapshot(json.dumps(valid)), valid)
        documents = ['', 'null', '[]', '{', ' ' * 16385,
                     '{"version":1,"version":1,"directories":{},"files":{}}']
        variants = []
        for field in ('version', 'directories', 'files'):
            changed = copy.deepcopy(valid)
            del changed[field]
            variants.append(changed)
        for version in (True, 1.0, 2, '1'):
            variants.append(dict(valid, version=version))
        variants.append(dict(valid, secret=MARKER))
        for group in ('directories', 'files'):
            changed = copy.deepcopy(valid)
            del changed[group][next(iter(changed[group]))]
            variants.append(changed)
            changed = copy.deepcopy(valid)
            changed[group]['/unreviewed'] = next(iter(changed[group].values()))
            variants.append(changed)
        path = next(iter(FILES))
        for field in FIELDS:
            for value in (True, 1.0, '1', None, -1, 2 ** 64):
                changed = copy.deepcopy(valid)
                changed['files'][path][field] = value
                variants.append(changed)
        changed = copy.deepcopy(valid)
        changed['files'][path]['contents'] = MARKER
        variants.append(changed)
        changed = copy.deepcopy(valid)
        del changed['files'][path]['ctime_ns']
        variants.append(changed)
        documents += [json.dumps(value) for value in variants]
        documents.append(json.dumps(valid).replace('"uid": 10001', '"uid": 10001, "uid": 10001', 1))
        for document in documents:
            with self.subTest(document=document[:80]), self.assertRaises(self.checks.Refusal) as raised:
                self.checks.parse_secret_snapshot(document)
            self.assertNotIn(MARKER, str(raised.exception))

    def test_snapshot_client_never_walks_secret_paths_and_fingerprint_detects_change(self):
        snapshot = self.checks.parse_secret_snapshot(json.dumps(self.snapshot()))
        with mock.patch.object(Path, 'resolve', side_effect=AssertionError('secret traversal')), \
                mock.patch.object(Path, 'lstat', side_effect=AssertionError('secret traversal')), \
                mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('content read')):
            for path, (uid, low, high) in FILES.items():
                before = self.checks.secret_metadata(path, REPO, uid, low, high, snapshot)
                changed = copy.deepcopy(snapshot)
                changed['files'][path]['ctime_ns'] += 1
                after = self.checks.secret_metadata(path, REPO, uid, low, high, changed)
                self.assertEqual(before[0], path)
                self.assertNotEqual(before, after)
            for path, uid, low, high in (('/etc/shadow', 0, 1, 8192),
                                         ('/etc/axonos/../shadow', 0, 1, 8192),
                                         (next(iter(FILES)), 0, 44, 4096)):
                with self.subTest(path=path), self.assertRaises(self.checks.Refusal):
                    self.checks.secret_metadata(path, REPO, uid, low, high, snapshot)

    def test_direct_diagnostic_permission_failure_is_sanitized(self):
        for operation in ('resolve', 'lstat'):
            with self.subTest(operation=operation), \
                    mock.patch.object(Path, 'resolve', lambda path: path), \
                    mock.patch.object(Path, operation, side_effect=PermissionError(errno.EACCES, MARKER)), \
                    self.assertRaises(self.checks.Refusal) as raised:
                self.checks.secret_metadata(next(iter(FILES)), REPO, 10001, 44, 4096)
            self.assertIn('current privileges', str(raised.exception))
            self.assertNotIn(MARKER, str(raised.exception))

    @unittest.skipIf(os.geteuid() == 0, 'requires an unprivileged DAC enforcement check')
    def test_real_unprivileged_search_denial_has_sanitized_refusal(self):
        # Real EACCES without host-root privileges. This is supplementary to,
        # not a substitute for, the separate root:root 0700 chroot fixture.
        with tempfile.TemporaryDirectory(prefix='.secret-search-fixture-', dir=REPO) as directory:
            parent = Path(directory) / 'protected'
            parent.mkdir(mode=0o700)
            target = parent / 'synthetic-key'
            target.write_bytes(b'synthetic-only' * 5)
            target.chmod(0o600)
            parent.chmod(0)
            try:
                with self.assertRaises(PermissionError) as original:
                    resolved = target.resolve()
                    resolved.lstat()
                self.assertEqual(original.exception.errno, errno.EACCES)
                with self.assertRaises(self.checks.Refusal) as corrected:
                    self.checks.secret_metadata(target, Path('/synthetic-build-context'),
                                                os.getuid(), 44, 4096)
                self.assertEqual(str(corrected.exception),
                                 'Secret metadata cannot be attested with current privileges')
                self.assertNotIn(str(target), str(corrected.exception))
            finally:
                parent.chmod(0o700)

    def test_helper_install_requires_fixed_root_owned_nonwritable_ancestors(self):
        self.assertEqual(self.checks.SECRET_METADATA_COMMAND, (
            '/usr/bin/sudo', '-n', '--', '/usr/bin/python3', '-I', '-S', '-B',
            '/usr/local/libexec/axonos-deploy-secret-metadata.py'))
        program = Path(self.checks.SECRET_METADATA_HELPER)
        values = {path: record(index, mode=stat.S_IFDIR | 0o755)
                  for index, path in enumerate(program.parents, 1)}
        values[program] = record(100, mode=stat.S_IFREG | 0o444, size=8192)

        def validate(entries):
            def lstat(path):
                return types.SimpleNamespace(**{'st_' + key: value for key, value in entries[path].items()})
            with mock.patch.object(Path, 'lstat', lstat), \
                    mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('content read')):
                self.checks.metadata_helper_contract()

        validate(values)
        for path in values:
            changes = [('uid', 1000), ('gid', 1000), ('mode', stat.S_IFLNK | 0o755)]
            if path == program:
                changes += [('mode', stat.S_IFREG | 0o644), ('nlink', 2), ('size', 0), ('size', 65537)]
            else:
                changes += [('mode', stat.S_IFDIR | 0o775), ('mode', stat.S_IFDIR | 0o757)]
            for field, value in changes:
                with self.subTest(path=path, field=field), self.assertRaises(self.checks.Refusal):
                    changed = copy.deepcopy(values)
                    changed[path][field] = value
                    validate(changed)


@unittest.skipUnless(os.geteuid() == 0, 'requires explicitly approved root-only synthetic chroot run')
class SecretMetadataRootFixtureTests(unittest.TestCase):
    """The parent touches synthetic paths only; fixed paths run after chroot."""

    @classmethod
    def setUpClass(cls):
        cls.helper = load_helper()
        cls.checks = load_checks(cls.helper)

    def fixture(self):
        temporary = tempfile.TemporaryDirectory(prefix='.secret-metadata-fixture-', dir=REPO)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        root.chmod(0o755)
        (root / 'etc').mkdir(mode=0o755)
        (root / 'etc').chmod(0o755)
        (root / 'etc/axonos').mkdir(mode=0o700)
        (root / 'etc/axonos').chmod(0o700)
        for path, (uid, _, _) in FILES.items():
            target = root / path.lstrip('/')
            target.write_bytes((MARKER * 2).encode()[:64])
            os.chown(target, uid, uid)
            target.chmod(0o600)
        return root

    def jailed(self, root, action, *, uid=0):
        read_descriptor, write_descriptor = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_descriptor)
            try:
                os.chroot(root)
                os.chdir('/')
                if uid:
                    os.setgroups([])
                    os.setgid(uid)
                    os.setuid(uid)
                payload = {'ok': True, 'result': action()}
            except BaseException as error:
                payload = {'ok': False, 'error_type': type(error).__name__}
            try:
                os.write(write_descriptor, json.dumps(payload).encode())
            finally:
                os._exit(0)
        os.close(write_descriptor)
        try:
            ready, _, _ = select.select([read_descriptor], [], [], 10)
            if not ready:
                os.kill(child, signal.SIGKILL)
                self.fail('synthetic chroot child timed out')
            chunks = []
            while True:
                chunk = os.read(read_descriptor, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            _, status = os.waitpid(child, 0)
            self.assertEqual(status, 0)
            wire = b''.join(chunks).decode()
            self.assertNotIn(MARKER, wire)
            result = json.loads(wire)
            self.assertTrue(result['ok'], result)
            return result['result']
        finally:
            os.close(read_descriptor)
            try:
                os.waitpid(child, os.WNOHANG)
            except ChildProcessError:
                pass

    def test_uid_1000_reproduces_resolve_and_lstat_denial_then_root_collects(self):
        root = self.fixture()

        def operator_attempt():
            result = {}
            for path in FILES:
                result[path] = {}

                def legacy_metadata():
                    original = Path(path)
                    if str(original.resolve()) != str(original):
                        raise AssertionError('unexpected canonical path')
                    return original.lstat()

                def readable_open():
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    os.close(descriptor)  # Never read, even if the permission boundary regresses.

                for name, operation in (('legacy', legacy_metadata), ('lstat', lambda: Path(path).lstat()),
                                        ('readable_open', readable_open)):
                    try:
                        operation()
                    except OSError as error:
                        result[path][name] = error.errno
                try:
                    self.checks.secret_metadata(path, REPO, *FILES[path])
                except self.checks.Refusal as error:
                    result[path]['diagnostic'] = str(error)
            return result

        denied = self.jailed(root, operator_attempt, uid=1000)
        for path in FILES:
            self.assertEqual({key: value for key, value in denied[path].items() if key != 'diagnostic'},
                             dict.fromkeys(('legacy', 'lstat', 'readable_open'), errno.EACCES))
            self.assertIn('current privileges', denied[path]['diagnostic'])
        original_open = os.open

        def metadata_only():
            def open_metadata(path, flags, *args, **kwargs):
                if not flags & os.O_PATH or not flags & os.O_NOFOLLOW:
                    raise AssertionError('helper attempted a readable open')
                return original_open(path, flags, *args, **kwargs)

            with mock.patch.object(self.helper.os, 'open', side_effect=open_metadata), \
                    mock.patch.object(self.helper.os, 'read', side_effect=AssertionError('content read')), \
                    mock.patch.object(self.helper.os, 'fdopen', side_effect=AssertionError('content read')), \
                    mock.patch('builtins.open', side_effect=AssertionError('content read')), \
                    mock.patch('io.open', side_effect=AssertionError('content read')), \
                    mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('content read')), \
                    mock.patch.object(sys, 'argv', [str(HELPER)]), \
                    redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                status = self.helper.main()
                if status or stderr.getvalue():
                    raise AssertionError('root fixture must run with python3 -I -S -B')
                return json.loads(stdout.getvalue())

        snapshot = self.jailed(root, metadata_only)
        self.assertEqual(set(snapshot['files']), set(FILES))
        self.assertEqual(snapshot['directories']['/etc/axonos']['mode'], stat.S_IFDIR | 0o700)
        for path, (uid, _, _) in FILES.items():
            self.assertEqual(snapshot['files'][path]['uid'], uid)
            self.assertEqual(snapshot['files'][path]['size'], 64)

        def client_uses_snapshot():
            parsed = self.checks.parse_secret_snapshot(json.dumps(snapshot))
            with mock.patch.object(Path, 'resolve', side_effect=AssertionError('secret traversal')), \
                    mock.patch.object(Path, 'lstat', side_effect=AssertionError('secret traversal')):
                return [self.checks.secret_metadata(path, REPO, *policy, parsed)[0]
                        for path, policy in FILES.items()]

        self.assertEqual(self.jailed(root, client_uses_snapshot, uid=1000), list(FILES))

    def test_real_invalid_leaf_metadata_is_refused(self):
        path = next(iter(FILES))
        for defect in ('symlink', 'owner', 'group', 'mode', 'hardlink', 'small', 'large', 'missing'):
            with self.subTest(defect=defect):
                root = self.fixture()
                target = root / path.lstrip('/')
                if defect == 'symlink':
                    target.unlink()
                    target.symlink_to('/etc/axonos/x-capi-hash-key')
                elif defect == 'owner':
                    os.chown(target, 1000, 10001)
                elif defect == 'group':
                    os.chown(target, 10001, 1000)
                elif defect == 'mode':
                    target.chmod(0o644)
                elif defect == 'hardlink':
                    os.link(target, root / 'extra-link')
                elif defect in ('small', 'large'):
                    target.write_bytes(b'x' * (43 if defect == 'small' else 4097))
                elif defect == 'missing':
                    target.unlink()

                def collect_refusal():
                    try:
                        self.helper.collect_snapshot()
                    except (self.helper.MetadataRefusal, OSError):
                        return True
                    return False

                self.assertTrue(self.jailed(root, collect_refusal))

    def test_real_parent_substitution_and_metadata_change_during_collection_refuse(self):
        for defect in ('parent-substitution', 'ctime-after-first-walk', 'ctime-after-second-walk'):
            with self.subTest(defect=defect):
                root = self.fixture()
                original_fstat = os.fstat

                def raced_collection():
                    calls = 0

                    def racing_fstat(descriptor):
                        nonlocal calls
                        value = original_fstat(descriptor)
                        calls += 1
                        trigger = 16 if defect == 'ctime-after-second-walk' else 8
                        if calls == trigger:
                            if defect == 'parent-substitution':
                                os.rename('/etc/axonos', '/etc/replaced')
                                os.mkdir('/etc/axonos', 0o700)
                            else:
                                os.utime(next(iter(FILES)), ns=(1234567890, 1234567891))
                        return value

                    try:
                        with mock.patch.object(self.helper.os, 'fstat', side_effect=racing_fstat):
                            self.helper.collect_snapshot()
                    except (self.helper.MetadataRefusal, OSError):
                        return True
                    return False

                self.assertTrue(self.jailed(root, raced_collection))


if __name__ == '__main__':
    unittest.main()
