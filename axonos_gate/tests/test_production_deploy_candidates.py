"""Candidate policy with classic/containerd-shaped metadata; no Docker daemon.

Containerd fixtures model Moby v28.5.2/docker-v29.5.1/docker-v29.5.2 image_inspect.go:
RepoTags contains actual stored names (including canonical names); RepoDigests
also includes repo@target for each tag. Classic digests are actual records.
"""

import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('deploy_candidate_policy_test', ROOT / 'scripts/deploy_production.py')
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class CandidateRetentionTests(unittest.TestCase):
    def setUp(self):
        self.images = {}
        self.references = set()
        self.commands = []
        self.failed_remove = False
        self.changed_tag = None
        self.add_canonical_on_reinspect = None
        self.inspect_counts = {}
        self.info = {'ServerVersion': '28.5.2', 'OSType': 'linux', 'Driver': 'overlay2',
                     'DriverStatus': [['Backing Filesystem', 'extfs']]}
        self.controller = deploy.Deployment.__new__(deploy.Deployment)
        self.controller.candidate = None
        self.controller.image_id = None
        self.controller.deployed = False
        self.controller.docker = self.docker

    def candidate(self, number, *, other_tags=(), owned=True):
        run = format(number, '024x')
        tag = deploy.CANDIDATE_PREFIX + run
        item = {'Id': 'sha256:' + format(number, '064x'), 'RepoTags': [tag, *other_tags],
                'RepoDigests': [], 'GraphDriver': {'Name': 'overlay2', 'Data': {}}, 'Config': {'Labels': {
                    deploy.CANDIDATE_OWNER: deploy.CANDIDATE_TOOL if owned else 'foreign',
                    deploy.CANDIDATE_RUN: run, deploy.CANDIDATE_CREATED: str(number)}}}
        if self.info['Driver'] == 'overlayfs':
            self.containerd_metadata(item)
        self.images[tag] = item
        return tag, item

    def containerd_metadata(self, item):
        item['Descriptor'] = {'mediaType': 'application/vnd.oci.image.index.v1+json',
                              'digest': item['Id'], 'size': 856}
        item.pop('GraphDriver', None)  # API >= 1.52 omits this for snapshotters.
        if self.info['ServerVersion'] == '28.5.2':
            item['GraphDriver'] = {'Name': 'overlayfs', 'Data': None}
        item['RepoDigests'] = sorted({ref if '@' in ref else ref.rsplit(':', 1)[0] + '@' + item['Id']
                                     for ref in item['RepoTags']})

    def use_containerd(self, version='29.5.1'):
        self.info = {'ServerVersion': version, 'OSType': 'linux', 'Driver': 'overlayfs',
                     'DriverStatus': [['driver-type', 'io.containerd.snapshotter.v1']]}
        for item in self.images.values():
            self.containerd_metadata(item)

    def canonical(self, item, repository):
        ref = repository + '@' + item['Id']
        if self.info['Driver'] == 'overlayfs':
            item['RepoTags'].append(ref)  # ACTUAL store record, not just RepoDigests.
            self.containerd_metadata(item)
        else:
            item['RepoDigests'].append(ref)
        return ref

    def docker(self, *args, **kwargs):
        self.commands.append(args)
        self.assertGreater(kwargs['timeout'], 0)
        self.assertLessEqual(kwargs['timeout'], 15)
        if args == ('info', '--format', '{{json .}}'):
            return json.dumps(self.info)
        if args[:2] == ('image', 'ls'):
            self.assertEqual(args[2:4], ('--filter', 'reference=axonos-deploy-candidate:*'))
            return '\n'.join(self.images)
        if args[:2] == ('image', 'inspect'):
            tag = args[2]
            self.inspect_counts[tag] = self.inspect_counts.get(tag, 0) + 1
            item = copy.deepcopy(self.images[tag])
            if tag == self.changed_tag and self.inspect_counts[tag] > 1:
                item['Id'] = 'sha256:' + 'f' * 64
            if tag == self.add_canonical_on_reinspect and self.inspect_counts[tag] > 1:
                self.canonical(item, 'axonos-deploy-candidate')
            return json.dumps([item])
        if args[:2] == ('image', 'rm'):
            self.assertEqual(args[2], '--no-prune')
            self.assertEqual(len(args), 4)
            self.assertRegex(args[3], r'^axonos-deploy-candidate:[a-f0-9]{24}$')
            if self.failed_remove:
                raise RuntimeError('SYNTHETIC_DIAGNOSTIC_MUST_NOT_PRINT')
            item = self.images[args[3]]
            stored = set(item['RepoTags'])
            if self.info['Driver'] == 'overlay2':
                stored.update(item['RepoDigests'])
            self.assertFalse(item['Id'] in self.references and stored == {args[3]})
            item['RepoTags'].remove(args[3])
            if self.info['Driver'] == 'overlayfs':
                self.containerd_metadata(item)
            del self.images[args[3]]
            return ''
        if args[:2] == ('ps', '-a'):
            self.assertEqual(args[2:5], ('-q', '--no-trunc', '--filter'))
            return 'a' * 64 if args[-1].removeprefix('ancestor=') in self.references else ''
        self.fail('Unexpected fake Docker invocation')

    def maintain(self, strict=False):
        with redirect_stdout(io.StringIO()) as output:
            self.controller.maintain_candidates(strict=strict)
        self.assertNotIn('SYNTHETIC_DIAGNOSTIC', output.getvalue())
        return output.getvalue()

    def removals(self):
        return [args[-1] for args in self.commands if args[:2] == ('image', 'rm')]

    def test_success_removes_only_temporary_tag_preserves_production_rollback_and_running_image(self):
        tag, item = self.candidate(1, other_tags=('axonos:latest', 'axonos:rollback-reviewed'))
        self.references.add(item['Id'])
        self.controller.candidate, self.controller.image_id = tag, item['Id']
        self.controller.deployed = True
        self.maintain()
        self.assertEqual(self.removals(), [tag])
        self.assertEqual(item['RepoTags'], ['axonos:latest', 'axonos:rollback-reviewed'])
        self.assertTrue(self.controller.deployed)

    def test_failed_candidate_is_retained(self):
        tag, item = self.candidate(1)
        self.controller.candidate, self.controller.image_id = tag, item['Id']
        self.assertIn('retained', self.maintain())
        self.assertEqual(self.removals(), [])
        self.assertIn(tag, self.images)

    def test_newest_three_failed_candidates_kept_oldest_tags_removed(self):
        tags = [self.candidate(number)[0] for number in range(1, 7)]
        self.maintain()
        self.assertEqual(set(self.images), set(tags[3:]))
        self.assertEqual(set(self.removals()), set(tags[:3]))

    def test_older_production_and_explicit_retention_images_keep_other_references(self):
        tag, item = self.candidate(1, other_tags=('axonos:latest', 'retention:keep'))
        self.references.add(item['Id'])
        for number in range(2, 5):
            self.candidate(number)
        self.maintain()
        self.assertEqual(self.removals(), [tag])
        self.assertEqual(item['RepoTags'], ['axonos:latest', 'retention:keep'])

    def test_only_reference_of_container_image_is_preserved_on_both_stores(self):
        for store in ('classic', '29.5.1', '29.5.2'):
            with self.subTest(store=store):
                self.setUp()
                if store != 'classic':
                    self.use_containerd(store)
                tag, item = self.candidate(1)
                self.references.add(item['Id'])
                for number in range(2, 5):
                    self.candidate(number)
                self.assertIn('WARNING', self.maintain())
                self.assertEqual(self.removals(), [])
                self.assertIn(tag, self.images)
                with redirect_stdout(io.StringIO()), self.assertRaises(deploy.Refusal):
                    self.controller.maintain_candidates(strict=True)

    def test_containerd_derived_digests_do_not_block_four_to_three_or_next_admission(self):
        for version in ('28.5.2', '29.5.1', '29.5.2'):
            with self.subTest(version=version):
                self.setUp()
                self.use_containerd(version)
                tags = [self.candidate(number)[0] for number in range(1, 5)]
                # Old code incorrectly protected every sole tag in this fixture.
                self.assertTrue(all(item['RepoDigests'] and len(item['RepoTags']) == 1
                                    for item in self.images.values()))
                self.maintain(strict=True)
                self.assertEqual(self.removals(), tags[:1])
                self.assertEqual(set(self.images), set(tags[1:]))
                self.maintain(strict=True)  # subsequent deployment is admitted
                self.assertEqual(self.removals(), tags[:1])

    def test_containerd_success_removes_tag_preserving_latest_rollback_and_container(self):
        self.use_containerd()
        tag, item = self.candidate(1, other_tags=('axonos:latest', 'retention:rollback'))
        self.references.add(item['Id'])
        self.controller.candidate, self.controller.image_id = tag, item['Id']
        self.controller.deployed = True
        self.maintain()
        self.assertEqual(self.removals(), [tag])
        self.assertEqual(item['RepoTags'], ['axonos:latest', 'retention:rollback'])
        self.assertEqual(item['RepoDigests'], ['axonos@' + item['Id'], 'retention@' + item['Id']])

    def test_containerd_older_latest_or_explicit_rollback_preserves_image_not_temporary_alias(self):
        for anchor in ('axonos:latest', 'retention:rollback'):
            with self.subTest(anchor=anchor):
                self.setUp()
                self.use_containerd()
                tag, item = self.candidate(1, other_tags=(anchor,))
                for number in range(2, 5):
                    self.candidate(number)
                self.maintain(strict=True)
                self.assertEqual(self.removals(), [tag])
                self.assertEqual(item['RepoTags'], [anchor])
                self.assertEqual(len(self.images), 3)

    def test_same_repository_canonical_protected_even_with_unrelated_tag_on_both_stores(self):
        for store in ('classic', '29.5.1', '29.5.2'):
            for anchors in ((), ('axonos:latest',), ('retention:rollback',)):
                with self.subTest(store=store, anchors=anchors):
                    self.setUp()
                    if store != 'classic':
                        self.use_containerd(store)
                    tag, item = self.candidate(1, other_tags=anchors)
                    digest = self.canonical(item, 'axonos-deploy-candidate')
                    for number in range(2, 5):
                        self.candidate(number)
                    self.assertIn('same-repository', self.maintain())
                    self.assertEqual(self.removals(), [])
                    self.assertIn(digest, item['RepoDigests'])
                    self.assertIn(tag, self.images)
                    with redirect_stdout(io.StringIO()), self.assertRaises(deploy.Refusal):
                        self.controller.maintain_candidates(strict=True)

    def test_same_repository_canonical_survives_when_another_same_repository_tag_remains(self):
        for store in ('classic', '29.5.1', '29.5.2'):
            with self.subTest(store=store):
                self.setUp()
                if store != 'classic':
                    self.use_containerd(store)
                anchor = 'axonos-deploy-candidate:manual-retention'
                tag, item = self.candidate(1, other_tags=(anchor,))
                digest = self.canonical(item, 'axonos-deploy-candidate')
                for number in range(2, 5):
                    self.candidate(number)
                self.maintain(strict=True)
                self.assertEqual(self.removals(), [tag])
                self.assertIn(anchor, item['RepoTags'])
                self.assertIn(digest, item['RepoDigests'])

    def test_different_repository_canonical_survives_and_does_not_block_cleanup(self):
        for store in ('classic', '29.5.1', '29.5.2'):
            with self.subTest(store=store):
                self.setUp()
                if store != 'classic':
                    self.use_containerd(store)
                tag, item = self.candidate(1)
                digest = self.canonical(item, 'registry.example:5000/retention/image')
                self.references.add(item['Id'])
                for number in range(2, 5):
                    self.candidate(number)
                self.maintain(strict=True)
                self.assertEqual(self.removals(), [tag])
                self.assertIn(digest, item['RepoDigests'])
                if store != 'classic':
                    self.assertEqual(item['RepoTags'], [digest])

    def test_docker_hub_repository_aliases_cannot_bypass_canonical_protection(self):
        for repo in ('docker.io/library/axonos-deploy-candidate', 'index.docker.io/axonos-deploy-candidate',
                     'library/axonos-deploy-candidate'):
            with self.subTest(repo=repo):
                self.setUp()
                self.use_containerd()
                tag, item = self.candidate(1, other_tags=('axonos:latest',))
                self.canonical(item, repo)
                for number in range(2, 5):
                    self.candidate(number)
                self.assertIn('same-repository', self.maintain())
                self.assertEqual(self.removals(), [])

    def test_canonical_added_since_inventory_is_protected_on_final_reinspection(self):
        self.use_containerd()
        tag, _ = self.candidate(1, other_tags=('axonos:latest',))
        self.add_canonical_on_reinspect = tag
        for number in range(2, 5):
            self.candidate(number)
        with redirect_stdout(io.StringIO()), self.assertRaises(deploy.Refusal):
            self.controller.maintain_candidates(strict=True)
        self.assertEqual(self.inspect_counts[tag], 2)
        self.assertEqual(self.removals(), [])

    def test_unknown_backend_or_version_refuses_even_empty_inventory(self):
        cases = ({'ServerVersion': '29.5.3'}, {'ServerVersion': '28.5.2-custom'}, {'OSType': 'windows'},
                 {'Driver': 'btrfs'}, {'Driver': 'overlayfs'}, {'DriverStatus': None},
                 {'DriverStatus': [['driver-type', 'io.containerd.snapshotter.v1']]},
                 {'DriverStatus': [['driver-type', 'unknown']]})
        for override in cases:
            with self.subTest(override=override):
                self.setUp()
                self.info.update(override)
                with redirect_stdout(io.StringIO()), self.assertRaises(deploy.Refusal):
                    self.controller.maintain_candidates(strict=True)
                self.assertEqual(self.commands, [('info', '--format', '{{json .}}')])

    def test_ambiguous_containerd_metadata_is_not_guessed_even_below_quota(self):
        corruptions = (
            lambda item: item.update(RepoDigests=[]),
            lambda item: item['RepoDigests'].append('unexplained@' + item['Id']),
            lambda item: item['RepoTags'].append('registry.example/image:tag@' + item['Id']),
            lambda item: item['RepoTags'].append('unexpected bare reference'),
            lambda item: item.update(RepoTags=None),
            lambda item: item.pop('Descriptor'),
            lambda item: item['Descriptor'].update(digest='sha256:' + 'f' * 64),
            lambda item: item['Descriptor'].update(mediaType='unknown'),
            lambda item: item.update(GraphDriver={'Name': 'overlay2'}),
        )
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                self.setUp()
                self.use_containerd()
                _, item = self.candidate(1)
                corrupt(item)
                with redirect_stdout(io.StringIO()), self.assertRaises(deploy.Refusal):
                    self.controller.maintain_candidates(strict=True)
                self.assertEqual(self.removals(), [])

    def test_namespace_alone_does_not_establish_ownership(self):
        foreign, _ = self.candidate(1, owned=False)
        legacy, item = self.candidate(2)
        item['Config']['Labels'] = {}
        self.images['axonos:latest'] = copy.deepcopy(item)
        self.images['axonos-deploy-candidate:manual-retention'] = copy.deepcopy(item)
        for number in range(3, 7):
            self.candidate(number)
        self.assertIn('legacy', self.maintain())
        self.assertIn(foreign, self.images)
        self.assertIn(legacy, self.images)
        self.assertIn('axonos:latest', self.images)
        self.assertEqual(self.removals(), [deploy.CANDIDATE_PREFIX + format(3, '024x')])

    def test_containerd_namespace_alone_does_not_establish_ownership(self):
        self.use_containerd()
        self.test_namespace_alone_does_not_establish_ownership()

    def test_matching_owner_wrong_run_label_is_not_removed(self):
        tag, item = self.candidate(1)
        item['Config']['Labels'][deploy.CANDIDATE_RUN] = 'f' * 24
        for number in range(2, 6):
            self.candidate(number)
        self.maintain()
        self.assertNotIn(tag, self.removals())

    def test_retag_race_refuses_removal(self):
        self.changed_tag, _ = self.candidate(1)
        for number in range(2, 5):
            self.candidate(number)
        self.assertIn('WARNING', self.maintain())
        self.assertEqual(self.removals(), [])

    def test_success_cleanup_failure_warns_without_reclassifying_deployment(self):
        tag, item = self.candidate(1, other_tags=('axonos:latest',))
        self.controller.candidate, self.controller.image_id = tag, item['Id']
        self.controller.deployed = True
        self.failed_remove = True
        self.assertIn('WARNING', self.maintain())
        self.assertTrue(self.controller.deployed)
        self.assertIn(tag, self.images)

    def test_prebuild_cleanup_failure_blocks_further_candidate_creation(self):
        for number in range(1, 5):
            self.candidate(number)
        self.failed_remove = True
        with redirect_stdout(io.StringIO()), self.assertRaises(deploy.Refusal):
            self.controller.maintain_candidates(strict=True)

    def test_inventory_work_is_capped(self):
        for number in range(1, deploy.CANDIDATE_SCAN_LIMIT + 2):
            self.candidate(number)
        with redirect_stdout(io.StringIO()), self.assertRaises(deploy.Refusal):
            self.controller.maintain_candidates(strict=True)
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(self.removals(), [])

    def test_overall_housekeeping_deadline_is_bounded(self):
        for number in range(1, 5):
            self.candidate(number)
        with patch.object(deploy.time, 'monotonic', side_effect=[0, 0, 61]):
            self.assertIn('WARNING', self.maintain())
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.removals(), [])


if __name__ == '__main__':
    unittest.main()
