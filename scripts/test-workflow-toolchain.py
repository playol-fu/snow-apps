#!/usr/bin/env python3
"""Run workflow toolchain detection with isolated Windows tool fixtures."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ('snow-shot-release.yml', 'snow-shot-test-from-qt.yml')


@unittest.skipUnless(os.name == 'nt', 'Windows command fixtures require Windows')
class WorkflowToolchain(unittest.TestCase):
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
            environment['PATH'] = str(directory) + os.pathsep + environment.get('PATH', '')
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
