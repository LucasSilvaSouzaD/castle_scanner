# GOTWiC Scanner no Windows (com o tema novo)

1. Instale o **Python 3.13** pelo python.org, marcando "Add python.exe to PATH" e "py launcher".
2. Instale o **Google Chrome** normalmente, em `C:\Program Files\Google\Chrome`.
3. Copie a pasta do projeto inteira para o PC. Não precisa copiar `.venv-mac` nem `RUN_MAC.command`.
4. Dê dois cliques em **`RUN_WINDOWS.bat`**. Na primeira vez ele cria o ambiente `.venv-win` e
   instala as dependências, o que leva alguns minutos. Depois use o atalho **"GOTWiC Scanner"** que ele cria na pasta.
5. Entre no jogo na janela do Chrome que ele abre (perfil separado, porta 9222).

Use `RUN_WINDOWS.bat`, e não o `RUN_GUI.bat`: os dois abrem o mesmo app, mas só o `RUN_WINDOWS.bat`
liga o tema novo, que fica em `macos_support\gotwic_macos_patch.py`. O atualizador oficial não mexe nessa pasta.

Ferramentas de WASM no Windows: `.venv-win\Scripts\python.exe wasm_tools\wasm_diagnose.py check`.

Para varreduras longas, deixe a suspensão do Windows em "Nunca" (Configurações > Sistema > Energia).
