"""Release trust boundaries, retained bytes and safe publication retries."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import conda_release as bundle
import publish_conda as publisher
import release_identity as identity

SHA = 'a' * 40
TAG = 'v0.1.0'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class ReleaseEvents(unittest.TestCase):
    def setUp(self):
        self.run = dict(id=42, status='completed', conclusion='success', event='push',
                        head_branch=TAG, head_sha=SHA, path=identity.WORKFLOW,
                        repository={'full_name': identity.REPOSITORY},
                        head_repository={'full_name': identity.REPOSITORY})

    def test_only_successful_same_repository_exact_tag_qualification_publishes(self):
        identity.select_run(self.run, SHA, TAG, publication=True)
        mutations = {'status': 'in_progress', 'conclusion': 'failure', 'event': 'pull_request',
                     'head_branch': 'master', 'head_sha': 'b' * 40, 'path': '.github/workflows/tests.yml',
                     'repository': {'full_name': 'fork/repo'}, 'head_repository': {'full_name': 'fork/repo'},
                     'id': True}
        for key, value in mutations.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                identity.select_run(dict(self.run, **{key: value}), SHA, TAG, publication=True)

    def test_manual_qualification_cannot_publish(self):
        manual = dict(self.run, event='workflow_dispatch', head_branch='master')
        identity.select_run(manual, SHA, TAG)
        with self.assertRaises(ValueError):
            identity.select_run(manual, SHA, TAG, publication=True)

    def test_artifact_selection_rejects_ambiguous_expired_or_empty(self):
        artifact = dict(id=7, name=identity.ARTIFACT, size_in_bytes=4, expired=False)
        self.assertEqual(identity.select_artifact([artifact])['id'], 7)
        for records in ([], [artifact, artifact], [dict(artifact, expired=True)],
                        [dict(artifact, size_in_bytes=0)], [dict(artifact, id=True)]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                identity.select_artifact(records)

    def test_dispatch_requires_selected_source_and_master_or_matching_tag(self):
        event = {'inputs': {}, 'ref': 'refs/heads/master', 'repository': {'full_name': identity.REPOSITORY}}
        identity.qualify_event(event, SHA, SHA, TAG)
        with self.assertRaises(ValueError):
            identity.qualify_event(event, SHA, 'b' * 40, TAG)
        with self.assertRaises(ValueError):
            identity.qualify_event(dict(event, ref='refs/heads/feature'), SHA, SHA, TAG)


class RetainedBundle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        self.candidate = self.bundle / 'candidate'
        release_dir = self.candidate / 'release'
        release_dir.mkdir(parents=True)
        (release_dir / 'helper.py').write_text('fixture helper')
        (release_dir / 'trusted.pem').write_text('fixture public key')
        sif = self.candidate / 'codex-hpc-linux-amd64.sif'
        sif.write_bytes(b'fixture signed image')
        self.image = {'architecture': 'linux/amd64', 'url': 'https://github.com/micknudsen/codex-hpc/releases/download/v0.1.0/codex-hpc-linux-amd64.sif',
                      'sha256': bundle.digest(sif), 'size': sif.stat().st_size, 'input_fingerprint': 'b' * 64}
        self.release = {'schema': 1, 'version': '0.1.0', 'image': self.image,
                        'helper_sha256': bundle.digest(release_dir / 'helper.py'),
                        'trust': {'sha256': bundle.digest(release_dir / 'trusted.pem')}}
        write(release_dir / 'release.json', self.release)
        write(self.candidate / 'selection.json', {'image': self.image, 'published': False, 'decision': 'new',
                                                'actions': dict.fromkeys(('build', 'sign', 'upload_image'), True)})
        write(self.candidate / 'provenance.json', {'signed_image_sha256': self.image['sha256'], 'fingerprint': self.image['input_fingerprint']})
        self.wheel = self.candidate / 'codex_hpc-0.1.0-py3-none-any.whl'
        with zipfile.ZipFile(self.wheel, 'w') as wheel:
            for path in release_dir.iterdir():
                wheel.write(path, 'codex_hpc/release/' + path.name)
        package_path = self.bundle / 'codex-hpc-0.1.0-py_0.conda'
        package_path.write_bytes(b'fixture conda bytes')
        self.package = dict(name='codex-hpc', version='0.1.0', build='py_0', subdir='noarch', fn=package_path.name, sha256=bundle.digest(package_path))
        write(self.bundle / 'qualification.json', dict(source=SHA, package=self.package, status='passed', suites=list(bundle.SUITES)))
        for suite in bundle.SUITES:
            write(self.bundle / 'conda-tests' / (suite + '.json'), dict(suite=suite, tests=1, failures=0, errors=0, skips=0))
        self.seal()

    def seal(self):
        write(self.bundle / 'release.json', dict(schema=1, repository=identity.REPOSITORY, channel=identity.CHANNEL,
                                               tag=TAG, source=SHA, package=self.package, files=bundle.files(self.bundle)))
        (self.bundle / 'SHA256SUMS').write_text(bundle.checksums(self.bundle))

    def test_complete_bundle_verifies_and_assets_are_deterministic(self):
        bundle.verify(self.bundle, TAG, SHA)
        for output in (self.root / 'one', self.root / 'two'):
            bundle.assets(self.bundle, output, TAG, SHA, 42)
        self.assertEqual(bundle.checksums(self.root / 'one'), bundle.checksums(self.root / 'two'))
        self.assertTrue((self.root / 'one/codex-hpc-linux-amd64.sif').is_file())

    def test_tampered_package_or_missing_test_is_rejected(self):
        (self.bundle / self.package['fn']).write_bytes(b'changed')
        with self.assertRaises(ValueError):
            bundle.verify(self.bundle, TAG, SHA)
        self.seal()
        with self.assertRaises(ValueError):
            bundle.verify(self.bundle, TAG, SHA)

    def test_even_resealed_skipped_or_empty_suite_is_rejected(self):
        for field in ('tests', 'skips', 'errors', 'failures'):
            result = dict(suite='setup', tests=1, failures=0, errors=0, skips=0)
            result[field] = 0 if field == 'tests' else 1
            write(self.bundle / 'conda-tests/setup.json', result)
            self.seal()
            with self.subTest(field=field), self.assertRaises(ValueError):
                bundle.verify(self.bundle, TAG, SHA)

    def test_wrong_source_channel_or_tag_is_rejected(self):
        for tag, source in (('v0.2.0', SHA), (TAG, 'b' * 40)):
            with self.assertRaises(ValueError):
                bundle.verify(self.bundle, tag, source)
        record = json.loads((self.bundle / 'release.json').read_text())
        record['channel'] = 'MOMA-AUH'
        write(self.bundle / 'release.json', record)
        (self.bundle / 'SHA256SUMS').write_text(bundle.checksums(self.bundle))
        with self.assertRaises(ValueError):
            bundle.verify(self.bundle, TAG, SHA)

    def test_reuse_assets_exclude_image_and_keep_original_url(self):
        selection = json.loads((self.candidate / 'selection.json').read_text())
        selection.update(decision='reuse', actions=dict.fromkeys(('build', 'sign', 'upload_image'), False))
        write(self.candidate / 'selection.json', selection)
        (self.candidate / 'codex-hpc-linux-amd64.sif').unlink()
        self.seal()
        output = self.root / 'assets'
        bundle.assets(self.bundle, output, TAG, SHA, 42)
        self.assertFalse((output / 'codex-hpc-linux-amd64.sif').exists())
        self.assertEqual(json.loads((output / 'image-release.json').read_text())['image'], self.image)

    def test_wheel_payload_must_match_external_trust_bundle(self):
        (self.candidate / 'release/trusted.pem').write_text('another key')
        self.seal()
        with self.assertRaises(ValueError):
            bundle.verify(self.bundle, TAG, SHA)

    def test_symlink_in_bundle_is_rejected(self):
        (self.bundle / 'link').symlink_to(self.wheel)
        with self.assertRaises(ValueError):
            bundle.verify(self.bundle, TAG, SHA)


class PublicationRetries(unittest.TestCase):
    def test_partial_draft_resume_uploads_only_missing_bytes_and_verifies_all(self):
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary) / 'assets'
            assets.mkdir()
            (assets / 'package.conda').write_bytes(b'qualified package')
            (assets / 'SHA256SUMS').write_bytes(b'qualified checksums')
            remote = {'package.conda': b'qualified package'}
            uploads = []
            downloads = []

            def gh(*args):
                if args[1] == 'upload':
                    path = Path(args[3])
                    uploads.append(path.name)
                    remote[path.name] = path.read_bytes()
                else:
                    self.assertEqual(args[1], 'download')
                    name = args[args.index('--pattern') + 1]
                    downloads.append(name)
                    (Path(args[args.index('--dir') + 1]) / name).write_bytes(remote[name])

            with patch.object(publisher, 'draft', return_value={'assets': [{'name': 'package.conda'}]}), patch.object(publisher, 'gh', side_effect=gh):
                publisher.attach(TAG, assets)
            self.assertEqual(uploads, ['SHA256SUMS'])
            self.assertEqual(set(downloads), {'package.conda', 'SHA256SUMS'})

    def test_conflicting_draft_asset_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary) / 'assets'
            assets.mkdir()
            (assets / 'package.conda').write_bytes(b'qualified package')

            def gh(*args):
                self.assertEqual(args[1], 'download')
                (Path(args[args.index('--dir') + 1]) / 'package.conda').write_bytes(b'conflicting package')

            with patch.object(publisher, 'draft', return_value={'assets': [{'name': 'package.conda'}]}), patch.object(publisher, 'gh', side_effect=gh), self.assertRaises(ValueError):
                publisher.attach(TAG, assets)

    def test_only_confirmed_channel_404_is_absence(self):
        package = dict(version='0.1.0', fn='codex-hpc-0.1.0-py_0.conda')
        for status in (404, 401, 403, 500):
            error = HTTPError('https://api.anaconda.org', status, 'fixture', {}, None)
            with self.subTest(status=status), patch.object(publisher, 'urlopen', side_effect=error):
                if status == 404:
                    self.assertFalse(publisher.channel_package(package))
                else:
                    with self.assertRaises(ValueError):
                        publisher.channel_package(package)

    def test_identical_channel_package_is_not_uploaded_again(self):
        with patch.object(publisher, 'channel_package', return_value=True), patch.object(publisher.subprocess, 'run') as run:
            publisher.ensure_channel(Path('/bundle'), {}, 'conda')
            run.assert_not_called()

    def test_lost_upload_response_requires_exact_remote_readback(self):
        with patch.object(publisher, 'channel_package', side_effect=[False, True]), patch.dict(publisher.os.environ, {'ANACONDA_API_TOKEN': 'fixture'}), patch.object(publisher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as run:
            publisher.ensure_channel(Path('/bundle'), {'fn': 'package.conda'}, 'conda')
            self.assertIn('micknudsen', run.call_args.args[0])
            self.assertIn('--fail', run.call_args.args[0])
            self.assertNotIn('--force', run.call_args.args[0])

    def test_published_github_release_is_never_mutated(self):
        response = subprocess.CompletedProcess([], 0, json.dumps(dict(draft=False, tag_name=TAG)), '')
        with patch.object(publisher.subprocess, 'run', return_value=response), self.assertRaises(ValueError):
            publisher.draft(TAG)

    def test_github_permission_failure_is_not_missing_draft(self):
        response = subprocess.CompletedProcess([], 1, '', 'HTTP 403')
        with patch.object(publisher.subprocess, 'run', return_value=response), self.assertRaises(ValueError):
            publisher.draft(TAG)


if __name__ == '__main__':
    unittest.main()
