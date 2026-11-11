$ErrorActionPreference = "Stop"

function Install-Uv {
  # Save the installer to disk first so the exact payload can be audited, and
  # refuse an empty or unexpected response. Piping a live network response
  # straight into Invoke-Expression runs whatever the endpoint returns.
  $installer = Join-Path $env:TEMP "uv-install.ps1"
  $installerScript = Invoke-RestMethod -Uri "https://astral.sh/uv/install.ps1"
  if (-not $installerScript -or $installerScript.Length -lt 100 -or $installerScript -notmatch 'uv') {
    throw "Refusing to run an unexpected uv installer payload"
  }
  Set-Content -Path $installer -Value $installerScript -Encoding UTF8
  Write-Host "uv installer saved to $installer"
  & $installer

  # Update the PATH environment variable for the current session
  $MachinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  if (-not $MachinePath) { $MachinePath = '' }
  $UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (-not $UserPath) { $UserPath = '' }
  $env:PATH = "$($MachinePath.TrimEnd(';'));$($UserPath.TrimEnd(';'))".Split(';', [System.StringSplitOptions]::RemoveEmptyEntries) -join ';'
}

if (Get-Command uv -ErrorAction SilentlyContinue) {
  $uvBin = "uv"
} else {
  Install-Uv
  $uvBin = "uv"
}

if (-not (Get-Command $uvBin -ErrorAction SilentlyContinue)) {
  Write-Error "Error: uv not found after installation."
  exit 1
}

& $uvBin tool install --python 3.13 kimo-cli
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
  Write-Error "uv tool install failed (exit $LASTEXITCODE)."
  exit $LASTEXITCODE
}
