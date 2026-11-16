# Copies every photo and file from Emergent's file storage into the Railway
# "live" file storage, under the same names, then checks each one.
#
# Run from C:\Users\admin\Desktop\ICE:
#   powershell -ExecutionPolicy Bypass -File scripts\copy_files_to_live.ps1            (real copy)
#   powershell -ExecutionPolicy Bypass -File scripts\copy_files_to_live.ps1 -DryRun    (list only)
#
# The Emergent key is typed here, hidden, and used only for this run. It is
# never saved, never put into Railway, and never shown. The database and
# bucket settings are read from Railway (live environment) the same way.
# Safe to run again: files already copied are skipped; nothing is deleted.

param([switch]$DryRun)
$ErrorActionPreference = "Stop"

$envs = @{}
foreach ($line in (railway.cmd variables -e live -s api --kv)) {
    if ($line -match '^(MONGO_URL|S3_[A-Z_]+)=(.*)$') { $envs[$Matches[1]] = $Matches[2] }
}
if (-not $envs["MONGO_URL"] -or -not $envs["S3_BUCKET"]) { Write-Host "Could not read the live settings from Railway. Run 'railway.cmd login' first."; exit 1 }
$envs["DB_NAME"] = "ice_site_ops"

if (-not $DryRun) {
    $secure = Read-Host "Paste the EMERGENT_LLM_KEY from Emergent's secrets screen" -AsSecureString
    $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if (-not $key) { Write-Host "No key entered. Nothing copied."; exit 1 }
    $envs["EMERGENT_LLM_KEY"] = $key.Trim()
}

$outDir = Join-Path $env:USERPROFILE "ice-migration"
New-Item -ItemType Directory -Force $outDir | Out-Null
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$manifestWin = Join-Path $outDir ("file_copy_" + $(if ($DryRun) {"dryrun_"} else {""}) + $stamp + ".csv")
$manifestWsl = "/mnt/c" + ($manifestWin.Substring(2) -replace '\\', '/')
$cliArgs = @("--scan-all-collections", "--manifest", $manifestWsl)
if ($DryRun) { $cliArgs += "--dry-run" }

$payload = @{ env = $envs; argv = $cliArgs } | ConvertTo-Json -Compress
$payload | wsl -- /root/ice-testenv/venv/bin/python /mnt/c/Users/admin/Desktop/ICE/scripts/copy_files_wrapper.py
$payload = $null; $envs = $null; $key = $null
Write-Host "Manifest (one line per file): $manifestWin"
