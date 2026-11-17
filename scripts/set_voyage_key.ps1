# Puts the Voyage AI key (used by "Search everything") into Railway, on the
# api service. The key is typed here, hidden, checked with one tiny test call
# (the word "test", no project data), then sent straight to Railway. It is
# never saved to disk or shown on screen. No restart: the next deploy uses it.
#
# Run from C:\Users\admin\Desktop\ICE:
#   powershell -ExecutionPolicy Bypass -File scripts\set_voyage_key.ps1                (live + staging)
#   powershell -ExecutionPolicy Bypass -File scripts\set_voyage_key.ps1 -LiveOnly

param([switch]$LiveOnly)
$ErrorActionPreference = "Stop"

Write-Host "Where did you create the key?"
Write-Host "  1 = MongoDB Atlas (AI Models page)"
Write-Host "  2 = voyageai.com dashboard"
$src = (Read-Host "Type 1 or 2").Trim()
if ($src -eq "1") { $base = "https://ai.mongodb.com/v1" }
elseif ($src -eq "2") { $base = "https://api.voyageai.com/v1" }
else { Write-Host "Please type 1 or 2. Nothing changed."; exit 1 }

$secure = Read-Host "Paste the key" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
$plain = ($plain | Out-String).Trim()
if (-not $plain -or $plain.Length -lt 20 -or $plain -match "\s") { Write-Host "That doesn't look like a key. Nothing changed."; $plain = $null; exit 1 }

Write-Host "Checking the key with one tiny test call..."
try {
    $body = @{ input = @("test"); model = "voyage-4"; input_type = "query" } | ConvertTo-Json
    $r = Invoke-RestMethod -Method Post -Uri "$base/embeddings" -Headers @{ Authorization = "Bearer $plain" } -ContentType "application/json" -Body $body -TimeoutSec 30
    $dims = $r.data[0].embedding.Count
    Write-Host "Key works (voyage-4 returned $dims numbers)."
} catch {
    $code = $null; try { $code = $_.Exception.Response.StatusCode.value__ } catch {}
    Write-Host "The test call failed (HTTP $code). Check you picked the right option above and copied the whole key. Nothing changed."
    $plain = $null; exit 1
}

$envs = @("live")
if (-not $LiveOnly) { $envs += "staging" }
foreach ($e in $envs) {
    $plain | railway.cmd variable set -e $e -s api VOYAGE_API_KEY --stdin --skip-deploys | Out-Null
    railway.cmd variable set -e $e -s api "VOYAGE_API_BASE=$base" --skip-deploys | Out-Null
    Write-Host "Done: VOYAGE_API_KEY and VOYAGE_API_BASE are saved on the $e api service (used from the next deploy; no restart now)."
}
$plain = $null
