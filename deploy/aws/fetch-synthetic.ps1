param(
    [Parameter(Mandatory=$true)][string]$Destinations,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [string]$Profile = 'costlens-demo'
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($OutputRoot)
if (Test-Path -LiteralPath $root) { throw 'Use a new output folder to preserve previous evidence' }
$registrations = Get-Content -LiteralPath $Destinations -Raw | ConvertFrom-Json
$sources = @('cur','cost-optimization-hub','compute-optimizer','trusted-advisor','savings-plans')
if (@($registrations).Count -ne 5 -or @($registrations.source | Select-Object -Unique).Count -ne 5) {
    throw 'Expected one registration per source'
}
$account = & aws sts get-caller-identity --profile $Profile --query Account --output text --no-cli-pager
if ($LASTEXITCODE -ne 0) { throw 'Cannot establish bucket owner' }
foreach ($registration in $registrations) {
    $source = $registration.source
    if ($source -notin $sources -or $registration.synthetic -ne $true) { throw 'Invalid synthetic source registration' }
    if ($registration.bucket -notmatch '^[a-z0-9.-]+$' -or $registration.prefix -notmatch '^runs/[a-z0-9-]+$') {
        throw 'Invalid destination'
    }
    $folder = Join-Path $root $source
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    $registration | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $folder 'source-location.json') -Encoding utf8
    $manifestPath = Join-Path $folder 'manifest.json'
    & aws s3api get-object --bucket $registration.bucket --key "$($registration.prefix)/manifest.json" --expected-bucket-owner $account --profile $Profile --region $registration.region --no-cli-pager $manifestPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cannot read manifest for $source" }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.synthetic_data -ne $true -or $manifest.source -ne $source) { throw 'Unexpected source manifest' }
    foreach ($entry in $manifest.files) {
        if ($entry.key -notmatch '^[a-z0-9_=-]+/part-[0-9]+\.parquet$') { throw 'Unexpected data key' }
        $localPath = Join-Path $folder $entry.key
        New-Item -ItemType Directory -Path (Split-Path -Parent $localPath) -Force | Out-Null
        & aws s3api get-object --bucket $registration.bucket --key "$($registration.prefix)/$($entry.key)" --expected-bucket-owner $account --profile $Profile --region $registration.region --no-cli-pager $localPath | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Cannot read data for $source" }
        if ((Get-FileHash -LiteralPath $localPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256) {
            throw "Checksum mismatch for $source"
        }
    }
    Write-Host "Fetched and verified: $source"
}
