param(
    [Parameter(Mandatory = $true)][string]$AssetId,
    [Parameter(Mandatory = $true)][string]$AssetRoot,
    [Parameter(Mandatory = $true)][string]$BaseRevision,
    [Parameter(Mandatory = $true)][string]$BaseSourceTreeIdentity,
    [Parameter(Mandatory = $true)][string]$SemanticSpecPath,
    [Parameter(Mandatory = $true)][string]$CampaignIdentity,
    [Parameter(Mandatory = $true)][string[]]$AcceptanceArtifactPaths,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $AssetRoot).Path
$forbiddenNames = @('.git', 'build', 'dist', '__pycache__', '.pytest_cache')
$forbidden = Get-ChildItem -LiteralPath $root -Force -Recurse | Where-Object {
    $_.Name -in $forbiddenNames -or $_.Name.EndsWith('.egg-info')
}
if ($forbidden.Count -gt 0) {
    throw "Asset contains generated or VCS content: $($forbidden[0].FullName)"
}

$files = Get-ChildItem -LiteralPath $root -File -Force -Recurse | Sort-Object {
    $_.FullName.Substring($root.Length).Replace('\', '/')
}
if ($files.Count -eq 0) {
    throw 'Asset source tree is empty'
}

$manifest = foreach ($file in $files) {
    if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Asset contains a reparse point: $($file.FullName)"
    }
    [pscustomobject]@{
        path = $file.FullName.Substring($root.Length).TrimStart('\').Replace('\', '/')
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        size = $file.Length
    }
}

$canonical = ($manifest | ForEach-Object { "$($_.path):$($_.sha256)`n" }) -join ''
$treeHash = [Convert]::ToHexString(
    [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($canonical))
).ToLowerInvariant()
$semanticSpecHash = (Get-FileHash -LiteralPath $SemanticSpecPath -Algorithm SHA256).Hash.ToLowerInvariant()
$acceptanceArtifacts = foreach ($artifactPath in $AcceptanceArtifactPaths) {
    $artifact = (Resolve-Path -LiteralPath $artifactPath).Path
    [pscustomobject]@{
        path = $artifact
        sha256 = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
$record = [ordered]@{
    asset_id = $AssetId
    asset_origin = 'RECONSTRUCTED_FROM_FROZEN_SEMANTIC_SPEC'
    campaign_identity = $CampaignIdentity
    semantic_spec_version = 'task3-semantics-v1'
    semantic_spec_hash = $semanticSpecHash
    base_revision = $BaseRevision
    base_source_tree_identity = $BaseSourceTreeIdentity
    implementation_source_identity = $treeHash
    content_hash = $treeHash
    file_count = $manifest.Count
    complete_source_manifest = @($manifest)
    acceptance_artifacts = @($acceptanceArtifacts)
    generated_at_utc = [DateTime]::UtcNow.ToString('o')
}

$destination = [IO.Path]::GetFullPath($OutputPath)
$parent = [IO.Path]::GetDirectoryName($destination)
[IO.Directory]::CreateDirectory($parent) | Out-Null
if ([IO.File]::Exists($destination)) {
    throw "Acceptance record already exists: $destination"
}
$temporary = Join-Path $parent ('.' + [IO.Path]::GetFileName($destination) + '.' + [guid]::NewGuid().ToString('N') + '.pending')
$json = $record | ConvertTo-Json -Depth 20
[IO.File]::WriteAllText($temporary, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
[IO.File]::Move($temporary, $destination)
Get-FileHash -LiteralPath $destination -Algorithm SHA256 | Select-Object Path, Hash
