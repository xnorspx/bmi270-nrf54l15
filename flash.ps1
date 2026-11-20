# Flash the BMI270 app to the XIAO nRF54L15 over USB.
#
# No external probe is needed: the board carries a SAMD11 acting as a CMSIS-DAP
# debugger on the same USB-C connector, so `west flash` talks to it directly.
# Close any serial monitor first - it holds the port.

$ErrorActionPreference = 'Stop'

$NcsToolchain = 'D:\Applications\ncs\toolchains\936afb6332'
$NcsWorkspace = 'D:\Applications\ncs\nrf-bm\v2.0.1'
$AppDir       = $PSScriptRoot
$BuildDir     = Join-Path $AppDir 'build'

$nrfutil = Join-Path $NcsToolchain 'nrfutil\bin\nrfutil.exe'
if (-not (Test-Path $nrfutil)) { throw "nrfutil not found at $nrfutil" }
if (-not (Test-Path (Join-Path $BuildDir 'zephyr\zephyr.elf'))) {
    throw "no build found - run .\build.ps1 first"
}

& $nrfutil toolchain-manager launch --toolchain-path $NcsToolchain --chdir $NcsWorkspace -- `
    west flash -d $BuildDir --runner openocd

exit $LASTEXITCODE
