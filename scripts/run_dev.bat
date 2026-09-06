@echo off
rem Double-clickable wrapper around run_dev.ps1.
rem
rem PowerShell refuses to run an unsigned .ps1 under the default execution
rem policy, which makes the script awkward to launch from Explorer or a plain
rem cmd prompt. This bypasses the policy for this one invocation only and
rem passes any arguments straight through:
rem
rem     scripts\run_dev.bat
rem     scripts\run_dev.bat -Port 1400 -NoDemo

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_dev.ps1" %*
