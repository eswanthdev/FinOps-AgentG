param(
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [string]$Profile = 'costlens-demo',
    [string]$Region = 'eu-central-1',
    [string]$RunId = '20261105-v1'
)
$ErrorActionPreference = 'Stop'
if ($RunId -notmatch '^[a-z0-9-]{1,40}$') { throw 'Invalid RunId' }
function Invoke-AwsChecked {
    param([string[]]$Arguments)
    $result = & aws @Arguments --profile $Profile --region $Region --no-cli-pager
    if ($LASTEXITCODE -ne 0) { throw "AWS command failed: $($Arguments[0]) $($Arguments[1])" }
    return $result
}
$identity = (Invoke-AwsChecked -Arguments @('sts','get-caller-identity','--output','json')) | ConvertFrom-Json
$account = $identity.Account
$root = (Resolve-Path -LiteralPath $DataRoot).Path
$sources = @('cur','cost-optimization-hub','compute-optimizer','trusted-advisor','savings-plans')
$destinations = @()
foreach ($source in $sources) {
    $bucket = "costlens-synthetic-$source-$account"
    $manifestPath = Join-Path $root "$source/manifest.json"
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.synthetic_data -ne $true -or $manifest.source -ne $source) { throw "Not synthetic: $source" }
    foreach ($entry in $manifest.files) {
        $localFile = Join-Path $root "$source/$($entry.key)"
        if ((Get-FileHash -LiteralPath $localFile -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256) {
            throw "Local checksum mismatch: $localFile"
        }
    }
    # Only create new buckets. Never reconfigure an existing bucket on a rerun.
    Invoke-AwsChecked -Arguments @('s3api','create-bucket','--bucket',$bucket,'--create-bucket-configuration',"LocationConstraint=$Region") | Out-Null
    Invoke-AwsChecked -Arguments @('s3api','put-public-access-block','--bucket',$bucket,'--expected-bucket-owner',$account,
        '--public-access-block-configuration','BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true') | Out-Null
    Invoke-AwsChecked -Arguments @('s3api','put-bucket-encryption','--bucket',$bucket,'--expected-bucket-owner',$account,
        '--server-side-encryption-configuration','{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}') | Out-Null
    Invoke-AwsChecked -Arguments @('s3api','put-bucket-versioning','--bucket',$bucket,'--expected-bucket-owner',$account,
        '--versioning-configuration','Status=Enabled') | Out-Null
    Invoke-AwsChecked -Arguments @('s3api','put-bucket-tagging','--bucket',$bucket,'--expected-bucket-owner',$account,
        '--tagging',"TagSet=[{Key=Project,Value=CostLens},{Key=DataClass,Value=Synthetic},{Key=Source,Value=$source}]") | Out-Null
    $prefix = "runs/$RunId"
    foreach ($entry in $manifest.files) {
        $localFile = Join-Path $root "$source/$($entry.key)"
        Invoke-AwsChecked -Arguments @('s3api','put-object','--bucket',$bucket,'--expected-bucket-owner',$account,
            '--key',"$prefix/$($entry.key)",'--body',$localFile,'--server-side-encryption','AES256',
            '--metadata',"synthetic=true,sha256=$($entry.sha256),source=$source",'--if-none-match','*') | Out-Null
        $verifyPath = Join-Path $root "verified/$source/$($entry.key)"
        New-Item -ItemType Directory -Path (Split-Path -Parent $verifyPath) -Force | Out-Null
        Invoke-AwsChecked -Arguments @('s3api','get-object','--bucket',$bucket,'--expected-bucket-owner',$account,
            '--key',"$prefix/$($entry.key)",$verifyPath) | Out-Null
        if ((Get-FileHash -LiteralPath $verifyPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256) {
            throw "S3 round-trip checksum mismatch: $source"
        }
    }
    # Publish the manifest only after data uploads and round-trip verification.
    Invoke-AwsChecked -Arguments @('s3api','put-object','--bucket',$bucket,'--expected-bucket-owner',$account,
        '--key',"$prefix/manifest.json",'--body',$manifestPath,'--server-side-encryption','AES256',
        '--metadata',"synthetic=true,source=$source",'--if-none-match','*') | Out-Null
    $verifiedManifest = Join-Path $root "verified/$source/manifest.json"
    Copy-Item -LiteralPath $manifestPath -Destination $verifiedManifest
    $destinations += [pscustomobject]@{source=$source;bucket=$bucket;prefix=$prefix;region=$Region;synthetic=$true}
    Write-Host "Verified synthetic dataset: s3://$bucket/$prefix/"
    $destinations | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 's3-destinations.json') -Encoding utf8
}
