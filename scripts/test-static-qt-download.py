#!/usr/bin/env python3
"""Exercise the Qt downloader against a local HTTP fixture without build tools."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = b'Qt source download fixture\x00\xff'


class DownloadHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, format, *args):
        pass


class StaticQtDownload(unittest.TestCase):
    def test_missing_destination_parent_and_existing_file(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if not shell:
            self.skipTest('PowerShell is required to exercise the Qt downloader')
        server = ThreadingHTTPServer(('127.0.0.1', 0), DownloadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        (ROOT / 'build').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='qt-download-', dir=ROOT / 'build') as fixture:
            destination = Path(fixture) / 'missing parent' / 'nested' / 'source.tar.xz'
            environment = os.environ.copy()
            environment.update({
                'SNOW_DOWNLOAD_SCRIPT': str(ROOT / 'scripts/build-static-qt.ps1'),
                'SNOW_DOWNLOAD_DESTINATION': str(destination),
                'SNOW_DOWNLOAD_URI': f'http://127.0.0.1:{server.server_port}/source',
            })
            command = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    $env:SNOW_DOWNLOAD_SCRIPT, [ref]$null, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw 'Qt builder must parse successfully.' }
$function = $ast.Find({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -ceq 'Save-RemoteFile'
}, $false)
if ($null -eq $function) { throw 'Qt downloader is missing.' }
Invoke-Expression $function.Extent.Text
$QtVersion = 'fixture'
Save-RemoteFile -Uri $env:SNOW_DOWNLOAD_URI -Destination $env:SNOW_DOWNLOAD_DESTINATION
# A second call exercises an existing parent and replaces an existing file.
[IO.File]::WriteAllText($env:SNOW_DOWNLOAD_DESTINATION, 'stale archive contents')
Save-RemoteFile -Uri $env:SNOW_DOWNLOAD_URI -Destination $env:SNOW_DOWNLOAD_DESTINATION
'''
            result = subprocess.run(
                [shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                 '-Command', command],
                env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(destination.read_bytes(), PAYLOAD)
            self.assertNotIn('IsSuccessStatusCode', result.stdout)


if __name__ == '__main__':
    unittest.main()
