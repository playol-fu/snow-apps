function Get-SnowNsisCompiler {
    [CmdletBinding()]
    param()

    $command = Get-Command makensis.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($command) { return $command.Source }

    foreach ($candidate in @(
            "${env:ProgramFiles(x86)}\NSIS\makensis.exe",
            "${env:ProgramFiles}\NSIS\makensis.exe")) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    throw "NSIS compiler 'makensis.exe' was not found. Put the portable compiler on PATH or install NSIS."
}
