#!/usr/bin/env python3
"""Focused portable-only build selection and mocked GitHub release checks."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/snow-shot-portable-release.yml'


class PortableRelease(unittest.TestCase):
    def test_workflow_only_builds_and_publishes_portable(self):
        source = WORKFLOW.read_text(encoding='utf-8')
        self.assertIn('contents: write', source)
        self.assertIn('package-snow-shot.ps1 -Architecture x64 -PortableOnly', source)
        self.assertIn('test-snow-shot-native-package.ps1 -Architecture x64 -PortableOnly', source)
        self.assertIn('snow-shot-$version-windows-x64-portable.zip', source)
        self.assertNotIn('Prepare NSIS', source)
        self.assertNotIn('Verify focused native architecture', source)
        self.assertNotIn('Verify focused updater', source)
        self.assertLess(source.index('Validate portable package'), source.index('Publish portable ZIP'))
        old = (ROOT / '.github/workflows/snow-shot-release.yml').read_text(encoding='utf-8')
        # Both workflows must restore the same expensive installed and Qt caches.
        for key in re.findall(r'key: (snow-(?:static-dependencies|qt-static)[^\n]+)', old):
            self.assertIn('key: ' + key, source)

    @unittest.skipUnless(os.name == 'nt', 'PowerShell command fixtures require Windows')
    def test_portable_mode_selects_full_target_without_nsis(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            self.skipTest('PowerShell is required')
        source = (ROOT / 'scripts/package-snow-shot.ps1').read_text(encoding='utf-8')
        nsis = re.search(r"if \(-not \$PortableOnly\) \{\n    \. .*snow-nsis-environment.*?\n\}", source, re.S).group()
        build = re.search(r'    \[string\[\]\]\$buildTargets = .*?\n    & cmake [^\n]+', source, re.S).group()
        (ROOT / 'build').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='portable-build-test-', dir=ROOT / 'build') as fixture:
            for portable in (True, False):
                script = f"$PortableOnly = ${str(portable).lower()}\n" + """
$ErrorActionPreference = 'Stop'
$buildDirectory = 'fixture-build'
$Parallelism = 4
$global:LASTEXITCODE = 0
function cmake { $args -join ' ' }
""" + build
                if portable:
                    # A portable package must never try to discover/install NSIS.
                    script += "\n" + nsis
                result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                                         'Bypass', '-Command', script], cwd=fixture,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('--target snow_shot', result.stdout)
                self.assertEqual('snow_shot_mini' in result.stdout, not portable)
        self.assertIn("if ($PortableOnly) { $configureArguments += '-DSNOW_APPS_BUILD_SNOW_SHOT_MINI=OFF' }", source)
        self.assertEqual(source.count('if ($PortableOnly) { continue }'), 2)
        self.assertIn('if (-not $PortableOnly) { . (Join-Path $PSScriptRoot "package-snow-shot-mini.ps1") }', source)
        native = (ROOT / 'scripts/test-snow-shot-native-package.ps1').read_text(encoding='utf-8')
        self.assertIn("$products = if ($PortableOnly) { @('snow-shot') }", native)
        self.assertIn("$variants = if ($PortableOnly) { @('portable') }", native)

    def test_release_upload_and_publish_with_mocked_github(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required')
        source = WORKFLOW.read_text(encoding='utf-8')
        script = textwrap.dedent(source.split('          script: |\n', 1)[1])
        (ROOT / 'build').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='portable-release-test-', dir=ROOT / 'build') as fixture:
            path = Path(fixture) / 'fixture.js'
            harness = """
const calls = [];
const mode = process.argv[2];
const repos = new Proxy({}, { get: (_, method) => async args => {
  calls.push({ method, args });
  if (method === 'getReleaseByTag' && mode !== 'existing') {
    const error = new Error('mock response');
    error.status = mode === 'denied' ? 403 : 404;
    throw error;
  }
  return { data: { id: 42, html_url: 'https://fixture.invalid/release' } };
}});
const github = { rest: { repos }, paginate: async () => mode === 'existing' ?
  [{ id: 11, name: process.env.PORTABLE_ASSET }] : [] };
const core = { summary: { addLink() { return this; }, async write() {} } };
const context = { repo: { owner: 'fixture', repo: 'snow-apps' }, sha: 'fixture-sha' };
const fakeRequire = () => ({ readFileSync: () => Buffer.from('verified zip fixture') });
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const execute = new AsyncFunction('require', 'github', 'context', 'core', SCRIPT);
execute(fakeRequire, github, context, core).then(() => {
  console.log(JSON.stringify({ calls, success: true }));
}).catch(error => {
  console.log(JSON.stringify({ calls, success: false, status: error.status }));
});
""".replace('SCRIPT', json.dumps(script))
            path.write_text(harness, encoding='utf-8')
            environment = {**os.environ, 'RELEASE_TAG': 'v1.2.4_snow-shot',
                           'RELEASE_VERSION': '1.2.4', 'PORTABLE_SHA256': 'fixture-sha256',
                           'PORTABLE_ASSET': 'snow-shot-1.2.4-windows-x64-portable.zip',
                           'PORTABLE_PATH': 'fixture.zip'}
            for mode in ('new', 'existing', 'denied'):
                with self.subTest(mode=mode):
                    result = subprocess.run([node, str(path), mode], env=environment,
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    data = json.loads(result.stdout)
                    methods = [call['method'] for call in data['calls']]
                    if mode == 'denied':
                        self.assertFalse(data['success'])
                        self.assertEqual(methods, ['getReleaseByTag'])
                        continue
                    self.assertTrue(data['success'])
                    self.assertEqual(methods.count('uploadReleaseAsset'), 1)
                    upload = next(call['args'] for call in data['calls'] if call['method'] == 'uploadReleaseAsset')
                    self.assertEqual(upload['name'], environment['PORTABLE_ASSET'])
                    self.assertEqual(bytes(upload['data']['data']), b'verified zip fixture')
                    publish = data['calls'][-1]
                    self.assertEqual(publish['method'], 'updateRelease')
                    self.assertFalse(publish['args']['draft'])
                    if mode == 'new':
                        create = next(call['args'] for call in data['calls'] if call['method'] == 'createRelease')
                        self.assertTrue(create['draft'])
                        self.assertEqual(create['target_commitish'], 'fixture-sha')
                    else:
                        self.assertIn('deleteReleaseAsset', methods)


if __name__ == '__main__':
    unittest.main()
