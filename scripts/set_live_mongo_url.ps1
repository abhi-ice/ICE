# Puts the Atlas connection string into Railway (live environment, api service).
# The password is typed here, hidden, and sent straight to Railway. It is never
# saved to disk or shown on screen.
#
# Run from C:\Users\admin\Desktop\ICE:  powershell -ExecutionPolicy Bypass -File scripts\set_live_mongo_url.ps1

$ErrorActionPreference = "Stop"
$secure = Read-Host "Paste the Atlas password for database user ice_app" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
if (-not $plain) { Write-Host "No password entered. Nothing changed."; exit 1 }
$enc = [Uri]::EscapeDataString($plain)
$uri = "mongodb+srv://ice_app:$enc@ice-prod.edeomua.mongodb.net/?retryWrites=true&w=majority&appName=ice-site-ops"
$uri | railway.cmd variable set -e live -s api MONGO_URL --stdin --skip-deploys | Out-Null
$plain = $null; $uri = $null; $enc = $null
Write-Host "Done: MONGO_URL is set on the live api service."
