$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repoRoot 'benchmarks\task3_v6\atomic64.c'
$outputDirectory = Join-Path $repoRoot '.tmp\task3_v6'
$output = Join-Path $outputDirectory 'task3_atomic.dll'
$compiler = Get-Command 'gcc.exe' -ErrorAction Stop

New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
& $compiler.Source -shared -O2 -Wall -Wextra -Werror $source -o $output
if ($LASTEXITCODE -ne 0) {
    throw "native helper compilation failed with exit code $LASTEXITCODE"
}

Get-FileHash -Algorithm SHA256 -LiteralPath $source,$output
