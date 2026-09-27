from __future__ import annotations

import subprocess
from pathlib import Path


SETUP = Path(__file__).resolve().parents[2] / "setup.ps1"


def test_setup_uses_main_service_python_for_patchright_install() -> None:
    text = SETUP.read_text(encoding="utf-8")

    assert "Invoke-RequiredNative" in text
    assert "Invoke-RequiredNative -FilePath $python -ArgumentList @('-m', 'patchright', 'install', 'chromium')" in text
    assert "patchright.exe" not in text
    assert "$exitCode = $LASTEXITCODE" in text
    assert "throw \"命令失败（退出码 $exitCode）：$FilePath$displayArgs\"" in text


def test_setup_script_parses_with_powershell() -> None:
    powershell = "pwsh.exe"
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{SETUP}', [ref]$null, [ref]$null)",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
