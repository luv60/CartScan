<#
.SYNOPSIS
    Builds the production release APK for QuickCompare Flutter application.
.DESCRIPTION
    Cleans Flutter build caches, resolves dependencies, and builds the production
    APK via 'flutter build apk --release'.
.PARAMETER ProjectDir
    Path to Flutter project directory. Defaults to quick_compare_app subdirectory.
.PARAMETER Clean
    Whether to run 'flutter clean' before building. Defaults to $true.
.PARAMETER CopyToRoot
    Whether to copy the built app-release.apk to the workspace root for easy download.
#>
param(
    [string]$ProjectDir = "$PSScriptRoot\quick_compare_app",
    [switch]$Clean = $true,
    [switch]$CopyToRoot = $true
)

$ErrorActionPreference = "Stop"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " QuickCompare Flutter Production Release APK Builder " -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Target Project Directory: $ProjectDir" -ForegroundColor Yellow

if (-not (Test-Path $ProjectDir)) {
    Write-Error "Project directory '$ProjectDir' does not exist."
    exit 1
}

Push-Location $ProjectDir
try {
    # 1. Check Flutter binary
    Write-Host "`n[1/4] Verifying Flutter SDK..." -ForegroundColor Cyan
    $flutterVersion = & flutter --version
    Write-Host ($flutterVersion | Select-Object -First 1) -ForegroundColor Gray

    # 2. Clean build caches
    if ($Clean) {
        Write-Host "`n[2/4] Cleaning Flutter build caches (flutter clean)..." -ForegroundColor Cyan
        & flutter clean
        if ($LASTEXITCODE -ne 0) {
            Write-Error "flutter clean failed with exit code $LASTEXITCODE"
            exit $LASTEXITCODE
        }
        Write-Host "Build caches cleaned successfully." -ForegroundColor Green
    } else {
        Write-Host "`n[2/4] Skipping build cache clean." -ForegroundColor Gray
    }

    # 3. Resolve dependencies
    Write-Host "`n[3/4] Resolving dependencies (flutter pub get)..." -ForegroundColor Cyan
    & flutter pub get
    if ($LASTEXITCODE -ne 0) {
        Write-Error "flutter pub get failed with exit code $LASTEXITCODE"
        exit $LASTEXITCODE
    }

    # 4. Build Production Release APK
    Write-Host "`n[4/4] Building production APK (flutter build apk --release)..." -ForegroundColor Cyan
    Write-Host "This may take a few moments..." -ForegroundColor Yellow
    & flutter build apk --release
    if ($LASTEXITCODE -ne 0) {
        Write-Error "flutter build apk --release failed with exit code $LASTEXITCODE"
        exit $LASTEXITCODE
    }

    # 5. Verify Build Output
    $apkPath = Join-Path $ProjectDir "build\app\outputs\flutter-apk\app-release.apk"
    if (Test-Path $apkPath) {
        $apkFile = Get-Item $apkPath
        $sizeMb = [math]::Round($apkFile.Length / 1MB, 2)
        $hash = (Get-FileHash -Path $apkPath -Algorithm SHA256).Hash

        Write-Host "`n============================================================" -ForegroundColor Green
        Write-Host " PRODUCTION APK BUILD SUCCESSFUL! " -ForegroundColor Green
        Write-Host "============================================================" -ForegroundColor Green
        Write-Host "APK Location : $apkPath" -ForegroundColor White
        Write-Host "APK Size     : $sizeMb MB ($($apkFile.Length) bytes)" -ForegroundColor White
        Write-Host "SHA-256 Hash : $hash" -ForegroundColor Gray

        if ($CopyToRoot) {
            $rootApkPath = Join-Path $PSScriptRoot "app-release.apk"
            Copy-Item -Path $apkPath -Destination $rootApkPath -Force
            Write-Host "Copied To    : $rootApkPath" -ForegroundColor Cyan
        }
    } else {
        Write-Error "Expected release APK not found at: $apkPath"
        exit 1
    }
}
finally {
    Pop-Location
}
