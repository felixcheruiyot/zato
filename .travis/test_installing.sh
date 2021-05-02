#!/bin/bash

set -ex

if [[ "${TRAVIS_OS_NAME}" == "windows" ]];then
    choco install make
    export TEMPDIR="$LOCALAPPDATA\\TempDir"
    powershell Add-MpPreference -ExclusionPath ${TEMPDIR}

    echo "DisableArchiveScanning..."
    powershell Start-Process -PassThru -Wait PowerShell -ArgumentList "'-Command Set-MpPreference -DisableArchiveScanning \$true'"
    echo "DisableBehaviorMonitoring..."
    powershell Start-Process -PassThru -Wait PowerShell -ArgumentList "'-Command Set-MpPreference -DisableBehaviorMonitoring \$true'"
    echo "DisableRealtimeMonitoring..."
    powershell Start-Process -PassThru -Wait PowerShell -ArgumentList "'-Command Set-MpPreference -DisableRealtimeMonitoring \$true'"

    powershell -Command 'Set-ExecutionPolicy -ExecutionPolicy RemoteSigned'
    cd $TRAVIS_BUILD_DIR/code
    [[ -f ./install.ps1 ]]
    powershell ./install.ps1
    cd $TRAVIS_BUILD_DIR
    # Adjusting path for Windows
    find -type f -name Makefile -exec sed -i -e 's|/bin|/Scripts|' {} \;
    make
fi