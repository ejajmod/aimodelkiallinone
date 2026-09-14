param(
    [string]$Image = 'aimodelki/aimodelki-allin1:comfy-baked-test',
    [string]$OutputDirectory = 'E:\R2\runtime',
    [string]$Version = '1.4.6-cu128'
)

$ErrorActionPreference = 'Stop'

if ($Version -notmatch '^[A-Za-z0-9._-]+$') {
    throw 'Version may contain only letters, digits, periods, hyphens and underscores.'
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$outputRoot = (Resolve-Path -LiteralPath $OutputDirectory).Path
$archiveName = "aimodelki-comfyui-$Version.tar.gz"
$archivePath = Join-Path $outputRoot $archiveName
$manifestPath = Join-Path $outputRoot "aimodelki-comfyui-$Version.manifest.json"

if (Test-Path -LiteralPath $archivePath) {
    throw "Archive already exists: $archivePath"
}

$imageId = (& docker image inspect $Image --format '{{.Id}}' 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "Docker image is not available: $Image. $imageId"
}

$mount = "type=bind,source=$outputRoot,target=/out"
& docker run --rm --mount $mount --entrypoint /bin/sh $Image -c "set -eu; test -f /opt/comfyui-baked/main.py; tar -C /opt -czf /out/$archiveName comfyui-baked; tar -tzf /out/$archiveName >/dev/null"
if ($LASTEXITCODE -ne 0) {
    throw 'Bundle export or archive verification failed.'
}

$archive = Get-Item -LiteralPath $archivePath
$sha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest = [ordered]@{
    schema_version = 1
    name = 'AIMODELKI ComfyUI runtime bundle'
    version = $Version
    image = $Image
    image_id = ($imageId | Out-String).Trim()
    archive = $archiveName
    size_bytes = $archive.Length
    sha256 = $sha256
    contents = 'ComfyUI source and baked custom nodes; excludes globally installed Python packages, CUDA libraries, and workflow models'
    created_utc = (Get-Date).ToUniversalTime().ToString('o')
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Output "Archive: $archivePath"
Write-Output "Manifest: $manifestPath"
Write-Output "SHA256: $sha256"
Write-Output 'Upload after configuring AWS CLI for your private Cloudflare R2 bucket:'
Write-Output ('aws s3 cp "{0}" s3://aimodelki-instant-models/runtime/{1} --metadata sha256={2} --endpoint-url $R2_ENDPOINT --profile aimodelki-r2-upload' -f $archivePath, $archiveName, $sha256)
Write-Output ('aws s3 cp "{0}" s3://aimodelki-instant-models/runtime/{1} --endpoint-url $R2_ENDPOINT --profile aimodelki-r2-upload' -f $manifestPath, (Split-Path -Leaf $manifestPath))
