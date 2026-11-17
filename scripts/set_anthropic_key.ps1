# Puts the Anthropic key into Railway (live environment, api service) so the
# AI features (assistant, photo analysis, voice notes, client template export)
# work. The key is typed here, hidden, and sent straight to Railway. It is
# never saved to disk or shown on screen. Railway restarts the api with it.
#
# Run from C:\Users\admin\Desktop\ICE:
#   powershell -ExecutionPolicy Bypass -File scripts\set_anthropic_key.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\set_anthropic_key.ps1 -AlsoStaging   (staging too)

param([switch]$AlsoStaging)
$ErrorActionPreference = "Stop"
$secure = Read-Host "Paste the Anthropic key (starts with sk-ant-)" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
$plain = ($plain | Out-String).Trim()
if (-not $plain) { Write-Host "No key entered. Nothing changed."; exit 1 }
if (-not $plain.StartsWith("sk-ant-")) { Write-Host "That doesn't look like an Anthropic key (should start with sk-ant-). Nothing changed."; $plain = $null; exit 1 }
$envs = @("live")
if ($AlsoStaging) { $envs += "staging" }
foreach ($e in $envs) {
    $plain | railway.cmd variable set -e $e -s api ANTHROPIC_API_KEY --stdin | Out-Null
    Write-Host "Done: ANTHROPIC_API_KEY is set on the $e api service (restarting now)."
}
$plain = $null
