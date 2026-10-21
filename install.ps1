# Install the latest ipx release binary on Windows.
#   irm https://raw.githubusercontent.com/inferenceport-ai/cli/main/install.ps1 | iex
# Options: $env:IPX_VERSION = "1.0.4"; $env:IPX_INSTALL_DIR = "$HOME\bin"
$ErrorActionPreference = "Stop"

$repo = "inferenceport-ai/cli"
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
$asset = "ipx-windows-$arch.exe"
$base = if ($env:IPX_VERSION) { "https://github.com/$repo/releases/download/v$($env:IPX_VERSION.TrimStart('v'))" } else { "https://github.com/$repo/releases/latest/download" }
$destDir = if ($env:IPX_INSTALL_DIR) { $env:IPX_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA "Programs\ipx" }

$tmp = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) ("ipx-" + [guid]::NewGuid()))
try {
    Write-Host "Downloading $asset ..."
    Invoke-WebRequest "$base/$asset" -OutFile (Join-Path $tmp "ipx.exe") -UseBasicParsing
    Invoke-WebRequest "$base/$asset.sha256" -OutFile (Join-Path $tmp "ipx.sha256") -UseBasicParsing
    $expected = (Get-Content (Join-Path $tmp "ipx.sha256") -Raw).Split(" ")[0].Trim().ToLower()
    $actual = (Get-FileHash (Join-Path $tmp "ipx.exe") -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) { throw "Checksum mismatch - refusing to install." }

    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    Move-Item -Force (Join-Path $tmp "ipx.exe") (Join-Path $destDir "ipx.exe")
    Write-Host "Installed ipx to $destDir\ipx.exe"

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($userPath -split ";") -notcontains $destDir) {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$destDir", "User")
        Write-Host "Added $destDir to your PATH (open a new terminal to use it)."
    }
    & (Join-Path $destDir "ipx.exe") --version
}
finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}




