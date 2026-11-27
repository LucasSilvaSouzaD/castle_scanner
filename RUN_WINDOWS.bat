@echo off
rem GOTWiC Scanner no Windows, com o mesmo tema do Mac.
rem Na primeira vez cria o ambiente .venv-win e instala as dependencias (alguns minutos).
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%ROOT%\.venv-win\Scripts\python.exe"

if exist "%PY%" goto venv_ok
echo Criando o ambiente Python .venv-win - so na primeira vez...
py -3.13 -m venv "%ROOT%\.venv-win"
if not errorlevel 1 goto venv_ok
echo.
echo Nao encontrei o Python 3.13.
echo Instale pelo python.org e marque "Add python.exe to PATH" e "py launcher".
pause
exit /b 1

:venv_ok
rem Liga o tema: o mesmo arquivo macos_support\gotwic_macos_patch.py usado no Mac.
"%PY%" -c "import sys, sysconfig, pathlib; site = pathlib.Path(sysconfig.get_paths()['purelib']); site.joinpath('gotwic_theme.pth').write_text(sys.argv[1] + '\nimport gotwic_macos_patch\n', encoding='utf-8')" "%ROOT%\macos_support"
if errorlevel 1 echo Aviso: nao consegui ligar o tema; o app abre com o visual antigo.

rem Atalho "GOTWiC Scanner" nesta pasta, apontando para este arquivo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:ROOT + '\GOTWiC Scanner.lnk'); $s.TargetPath = $env:ROOT + '\RUN_WINDOWS.bat'; $s.WorkingDirectory = $env:ROOT; $s.IconLocation = $env:ROOT + '\GOTWiC_Scanner.ico,0'; $s.Save()" >nul 2>&1

rem Mesmo inicio do RUN_GUI.bat: confere dependencias, abre o Chrome (porta 9222) e o app.
"%PY%" startup.py %*
if errorlevel 1 pause
