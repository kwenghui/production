param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$BackupDir
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$localZip = Join-Path ([IO.Path]::GetTempPath()) "sage-development-$stamp.zip"
$destZip = Join-Path $BackupDir "development-$stamp.zip"
$excludedDirs = @('.git','.venv','venv','env','__pycache__','node_modules','.pytest_cache','.ruff_cache','.mypy_cache','.tox','.nox','logs','log','.secrets','secrets','temp','tmp')
$excludedNames = @('.env','.sage_log_key','secrets.toml','credentials.json','service-account.json','id_rsa','id_ed25519','debug.txt','failed.txt','success.txt')
$excludedExt = @('.zip','.7z','.rar','.pem','.key','.p12','.pfx','.log','.pyc','.pyo')
try {
    $root = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\')
    $files = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force | Where-Object {
        $rel = $_.FullName.Substring($root.Length).TrimStart('\')
        $parts = $rel -split '\\'
        $blocked = $false
        foreach ($part in $parts) { if ($excludedDirs -contains $part) { $blocked = $true; break } }
        -not $blocked -and $excludedNames -notcontains $_.Name -and
        -not $_.Name.StartsWith('.env.',[StringComparison]::OrdinalIgnoreCase) -and
        -not $_.Name.StartsWith('debug.txt.',[StringComparison]::OrdinalIgnoreCase) -and
        $excludedExt -notcontains $_.Extension
    })
    if ($files.Count -eq 0) { throw 'No eligible backup files.' }
    $archive = [IO.Compression.ZipFile]::Open($localZip,[IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in $files) {
            $rel = $file.FullName.Substring($root.Length).TrimStart('\').Replace('\','/')
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive,$file.FullName,$rel,[IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    } finally { $archive.Dispose() }
    $archive = [IO.Compression.ZipFile]::OpenRead($localZip)
    try {
        if ($archive.Entries.Count -ne $files.Count) { throw 'ZIP entry count mismatch.' }
        $buffer = New-Object byte[] 65536
        foreach ($entry in $archive.Entries) {
            $stream = $entry.Open()
            try { while ($stream.Read($buffer,0,$buffer.Length) -gt 0) {} }
            finally { $stream.Dispose() }
        }
    } finally { $archive.Dispose() }
    Copy-Item -LiteralPath $localZip -Destination $destZip -ErrorAction Stop
    $a = (Get-FileHash -LiteralPath $localZip -Algorithm SHA256).Hash
    $b = (Get-FileHash -LiteralPath $destZip -Algorithm SHA256).Hash
    if ($a -ne $b) { throw 'OneDrive copy hash mismatch.' }
    Write-Host "VERIFIED: $destZip ($($files.Count) files)"
} catch {
    Write-Error "Backup failed: $_"
    exit 1
} finally {
    Remove-Item -LiteralPath $localZip -Force -ErrorAction SilentlyContinue
}
