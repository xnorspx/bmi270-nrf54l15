# Build the BMI270 app for XIAO nRF54L15.
#
# The NCS installation on this machine is not wired into a west workspace shell,
# so everything goes through nrfutil's toolchain launcher with an explicit
# toolchain path. Only the 936afb6332 bundle has a complete arm-zephyr-eabi gcc
# (the 4f5b6ad6dd one is missing it and carries Zephyr SDK 1.0.1).

$ErrorActionPreference = 'Stop'

$NcsToolchain = 'D:\Applications\ncs\toolchains\936afb6332'
$NcsWorkspace = 'D:\Applications\ncs\nrf-bm\v2.0.1'
$Board        = 'xiao_nrf54l15/nrf54l15/cpuapp'
$AppDir       = $PSScriptRoot
$BuildDir     = Join-Path $AppDir 'build'

$nrfutil = Join-Path $NcsToolchain 'nrfutil\bin\nrfutil.exe'
if (-not (Test-Path $nrfutil)) { throw "nrfutil not found at $nrfutil" }

& $nrfutil toolchain-manager launch --toolchain-path $NcsToolchain --chdir $NcsWorkspace -- `
    west build -p always --no-sysbuild -b $Board -d $BuildDir $AppDir

exit $LASTEXITCODE
