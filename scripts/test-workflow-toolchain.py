#!/usr/bin/env python3
"""Run workflow toolchain preparation with isolated Windows tool fixtures."""

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ('snow-shot-release.yml', 'snow-shot-test-from-qt.yml')


@unittest.skipUnless(os.name == 'nt', 'Windows command fixtures require Windows')
class WorkflowToolchain(unittest.TestCase):
    def test_nsis_mirrors_reject_corrupt_archives_before_extraction(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            self.skipTest('PowerShell is required')
        (ROOT / 'build').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='nsis-mirror-test-', dir=ROOT / 'build') as fixture:
            directory = Path(fixture)
            archive = directory / 'fixture.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('nsis-3.12/makensis.cmd',
                                '@echo off\necho v3.12-fixture\nexit /b 0\n')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            for workflow in WORKFLOWS:
                source = (ROOT / '.github/workflows' / workflow).read_text(encoding='utf-8')
                match = re.search(
                    r'      - name: Prepare NSIS packaging compiler on the runner\n'
                    r'.*?        run: \|\n((?:          [^\n]*\n|\n)+)', source, re.DOTALL,
                )
                self.assertIsNotNone(match, workflow)
                command = textwrap.dedent(match.group(1))
                command = re.sub(r"\$expected = '[a-f0-9]{64}'",
                                 f"$expected = '{digest}'", command)
                # Use a batch fixture rather than downloading or installing a compiler.
                command = command.replace('makensis.exe', 'makensis.cmd')
                for mode in ('corrupt-first', 'network-first', 'all-corrupt'):
                    with self.subTest(workflow=workflow, download=mode):
                        workspace = directory / (workflow.removesuffix('.yml') + '-' + mode)
                        (workspace / '.tools').mkdir(parents=True)
                        output = workspace / 'github-path.txt'
                        environment = {**os.environ, 'GITHUB_WORKSPACE': str(workspace),
                                       'GITHUB_PATH': str(output), 'NSIS_FIXTURE': str(archive),
                                       'NSIS_RESPONSE_MODE': mode}
                        mock = """
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$script:attempt = 0
function Invoke-WebRequest {
    param([string]$Uri, [string]$OutFile, [int]$TimeoutSec, [string]$UserAgent)
    $script:attempt += 1
    if ($script:attempt -eq 1 -and $env:NSIS_RESPONSE_MODE -eq 'network-first') {
        throw 'Simulated mirror timeout.'
    }
    if ($env:NSIS_RESPONSE_MODE -eq 'all-corrupt' -or $script:attempt -eq 1) {
        [IO.File]::WriteAllText($OutFile, '<html>Download unavailable</html>')
    } else {
        Copy-Item -LiteralPath $env:NSIS_FIXTURE -Destination $OutFile
    }
}
"""
                        result = subprocess.run([
                            shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                            '-Command', mock + command,
                        ], cwd=workspace, env=environment, capture_output=True, text=True, timeout=30)
                        compiler = workspace / '.tools/nsis-3.12/makensis.cmd'
                        if mode == 'all-corrupt':
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn('No verified NSIS portable archive', result.stderr)
                            self.assertFalse(compiler.exists())
                            self.assertFalse(output.exists())
                        else:
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            self.assertIn('v3.12-fixture', result.stdout)
                            self.assertTrue(compiler.exists())
                            data = output.read_bytes()
                            encoding = 'utf-16' if data.startswith(b'\xff\xfe') else 'utf-8-sig'
                            self.assertEqual(data.decode(encoding).strip(), str(compiler.parent))

    def test_dependency_cache_restores_into_an_existing_vcpkg_checkout(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        git = shutil.which('git')
        if not shell or not git:
            self.skipTest('PowerShell and Git are required')
        (ROOT / 'build').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='vcpkg-cache-test-', dir=ROOT / 'build') as fixture:
            directory = Path(fixture)
            upstream = directory / 'upstream'
            upstream.mkdir()
            subprocess.run([git, 'init', '--quiet', str(upstream)], check=True)
            (upstream / 'vcpkg-source.txt').write_text('pinned checkout fixture\n')
            subprocess.run([git, '-C', str(upstream), 'add', '.'], check=True)
            subprocess.run([
                git, '-C', str(upstream), '-c', 'user.name=Fixture',
                '-c', 'user.email=fixture@example.invalid', 'commit', '--quiet', '-m', 'fixture',
            ], check=True)
            bootstrap = (ROOT / 'scripts/bootstrap.ps1').read_text(encoding='utf-8')
            clone_guard = bootstrap.split('$vcpkgGitDirectory =', 1)[1].split(
                'if (-not (Test-Path -LiteralPath $vcpkgGitDirectory))', 1,
            )[0]
            for workflow in WORKFLOWS:
                with self.subTest(workflow=workflow):
                    source = (ROOT / '.github/workflows' / workflow).read_text(encoding='utf-8')
                    prepare = source.index('- name: Prepare repository-managed vcpkg checkout')
                    restore = source.index('- name: Restore static project dependencies')
                    self.assertLess(prepare, restore, 'Clone must precede dependency cache restore')
                    match = re.search(
                        r'      - name: Prepare repository-managed vcpkg checkout\n'
                        r'.*?        run: \|\n((?:          [^\n]*\n|\n)+)', source, re.DOTALL,
                    )
                    self.assertIsNotNone(match)
                    command = textwrap.dedent(match.group(1)).replace(
                        'https://github.com/microsoft/vcpkg.git', upstream.as_uri(),
                    )
                    workspace = directory / workflow.removesuffix('.yml')
                    (workspace / '.tools').mkdir(parents=True)
                    environment = {**os.environ, 'GITHUB_WORKSPACE': str(workspace)}
                    # Restore an installed library after the actual workflow clone step,
                    # then run bootstrap's real clone guard without installing any tools.
                    command += """
$installed = Join-Path $vcpkgRoot 'installed/static/x64-windows-static/lib'
[IO.Directory]::CreateDirectory($installed) | Out-Null
[IO.File]::WriteAllText((Join-Path $installed 'cached.lib'), 'cached dependency')
$vcpkgExe = Join-Path $vcpkgRoot 'vcpkg.exe'
function Invoke-Checked { throw 'Bootstrap unexpectedly tried to clone over cached dependencies.' }
$vcpkgGitDirectory =""" + clone_guard + """
git -C $vcpkgRoot rev-parse --verify HEAD
if ($LASTEXITCODE -ne 0) { throw 'Cached vcpkg is not a usable checkout.' }
if ((Get-Content -Raw (Join-Path $installed 'cached.lib')) -ne 'cached dependency') {
    throw 'Restored dependency was lost.'
}
"""
                    result = subprocess.run([
                        shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                        '-Command', "$ErrorActionPreference = 'Stop'\n" + command,
                    ], cwd=workspace, env=environment, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cmake_exit_status_with_initially_unset_last_exit_code(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            self.skipTest('PowerShell is required')
        (ROOT / 'build').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='toolchain-test-', dir=ROOT / 'build') as fixture:
            directory = Path(fixture)
            (directory / 'scripts').mkdir()
            (directory / 'cl.exe').write_bytes(b'compiler fingerprint fixture')
            (directory / 'scripts/snow-build-environment.ps1').write_text('''
Set-StrictMode -Version Latest
Remove-Variable LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue
function Add-SnowMsvcToolsToPath {
    param([string]$Architecture)
    $env:WindowsSDKVersion = '10.0.fixture'
    return (Get-Location).Path
}
''', encoding='utf-8')
            environment = os.environ.copy()
            # Get-Command -CommandType Application can return multiple PATH matches.
            # Keep a competing installation in the fixture even on PCs without CMake.
            competing = directory / 'other-cmake'
            competing.mkdir()
            (competing / 'cmake.cmd').write_text(
                '@echo off\necho incorrect competing CMake\nexit /b 9\n', encoding='ascii',
            )
            environment['PATH'] = os.pathsep.join(
                (str(directory), str(competing), environment.get('PATH', ''))
            )
            environment['SNOW_ARCHITECTURE'] = 'x64'
            output = directory / 'github-output.txt'
            environment['GITHUB_OUTPUT'] = str(output)
            for workflow in WORKFLOWS:
                source = (ROOT / '.github/workflows' / workflow).read_text(encoding='utf-8')
                match = re.search(
                    r'      - name: Identify installed build toolchain\n'
                    r'.*?        run: \|\n((?:          [^\n]*\n|\n)+)', source, re.DOTALL,
                )
                self.assertIsNotNone(match, workflow)
                command = textwrap.dedent(match.group(1))
                for exit_code in (0, 7):
                    with self.subTest(workflow=workflow, cmake_exit_code=exit_code):
                        (directory / 'cmake.cmd').write_text(
                            '@echo off\necho cmake version 4.2.3-fixture\n'
                            f'exit /b {exit_code}\n', encoding='ascii',
                        )
                        output.unlink(missing_ok=True)
                        result = subprocess.run(
                            [shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                             'Bypass', '-Command', "$ErrorActionPreference = 'Stop'\n" + command],
                            cwd=directory, env=environment, capture_output=True,
                            text=True, timeout=30,
                        )
                        if exit_code == 0:
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            # Windows PowerShell writes redirected output as UTF-16.
                            data = output.read_bytes()
                            encoding = 'utf-16' if data.startswith(b'\xff\xfe') else 'utf-8-sig'
                            self.assertIn('cmake-version-4.2.3-fixture', data.decode(encoding))
                        else:
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn('Could not identify CMake.', result.stdout + result.stderr)
                            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
