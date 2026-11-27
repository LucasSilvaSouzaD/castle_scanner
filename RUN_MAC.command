#!/bin/bash
# GOTWiC Scanner — launcher para macOS (equivalente ao RUN_GUI.bat).
# Dê dois cliques neste arquivo no Finder, ou rode:  ./RUN_MAC.command
#
# O que ele faz:
#   1. Escolhe um Python 3.11+ que tenha Tkinter (o updater do app usa Tkinter).
#   2. Cria/atualiza um ambiente virtual em .venv-mac e instala requirements_gui.txt.
#   3. Instala o patch de compatibilidade macOS (macos_support/) dentro do venv.
#   4. Abre o Google Chrome com depuração remota (porta 9222) num perfil separado.
#   5. Inicia o startup.py -> gotwic_gui.py, como no Windows.
#
# Variáveis opcionais:
#   GOTWIC_PYTHON=/caminho/python3   força um Python específico
#   GOTWIC_CDP_PORT=9222             porta de depuração do Chrome
#   GOTWIC_NO_CHROME=1               não abre o Chrome automaticamente
set -u
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
VENV="$ROOT/.venv-mac"
PORT="${GOTWIC_CDP_PORT:-9222}"
CHROME_PROFILE="$HOME/Library/Application Support/GOTWiC-Scanner-Chrome"

fail() { echo; echo "ERRO: $*"; echo; read -r -p "Pressione Enter para fechar..." _; exit 1; }

# ---------- 1. Python ----------
pick_python() {
  local candidates=()
  [ -n "${GOTWIC_PYTHON:-}" ] && candidates+=("$GOTWIC_PYTHON")
  for v in 3.13 3.12 3.11; do
    candidates+=("/Library/Frameworks/Python.framework/Versions/$v/bin/python$v")
    candidates+=("/opt/homebrew/bin/python$v" "/usr/local/bin/python$v")
    command -v "python$v" >/dev/null 2>&1 && candidates+=("$(command -v "python$v")")
  done
  command -v python3 >/dev/null 2>&1 && candidates+=("$(command -v python3)")
  local fallback=""
  for py in "${candidates[@]}"; do
    [ -x "$py" ] || continue
    "$py" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (3,14) else 1)' 2>/dev/null || continue
    if "$py" -c 'import tkinter' 2>/dev/null; then echo "$py"; return 0; fi
    [ -z "$fallback" ] && fallback="$py"
  done
  [ -n "$fallback" ] && { echo "NO_TK:$fallback"; return 0; }
  return 1
}

PY_CHOICE="$(pick_python)" || fail "Nenhum Python 3.11–3.13 encontrado.
Instale o Python 3.13 em https://www.python.org/downloads/macos/ (já vem com Tkinter)
ou via Homebrew:  brew install python@3.13 python-tk@3.13"

if [[ "$PY_CHOICE" == NO_TK:* ]]; then
  PY_CHOICE="${PY_CHOICE#NO_TK:}"
  echo "AVISO: $PY_CHOICE não tem Tkinter. O app abre, mas o atualizador automático vai falhar."
  echo "       Corrija com:  brew install python-tk@$("$PY_CHOICE" -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  echo "       (ou use o Python do python.org). Depois apague a pasta .venv-mac e rode de novo."
  echo
fi
echo "Python base: $PY_CHOICE ($("$PY_CHOICE" --version 2>&1))"

# ---------- 2. venv + dependências ----------
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c 'import sys' >/dev/null 2>&1; then
  echo "Criando ambiente virtual em .venv-mac ..."
  rm -rf "$VENV" 2>/dev/null
  "$PY_CHOICE" -m venv "$VENV" || fail "Não foi possível criar o ambiente virtual."
fi
PY="$VENV/bin/python"

REQ_HASH="$(shasum -a 256 requirements_gui.txt | cut -d' ' -f1)"
STAMP="$VENV/.requirements.sha256"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$REQ_HASH" ]; then
  echo "Instalando dependências (PySide6, websocket-client, lz4, pymysql, requests)..."
  "$PY" -m pip install --disable-pip-version-check -q --upgrade pip || fail "pip falhou."
  "$PY" -m pip install --disable-pip-version-check -r requirements_gui.txt || fail "Falha ao instalar dependências."
  echo "$REQ_HASH" > "$STAMP"
fi

# ---------- 3. patch macOS (via .pth no venv) ----------
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
printf '%s\nimport gotwic_macos_patch\n' "$ROOT/macos_support" > "$SITE/gotwic_macos.pth" \
  || fail "Não foi possível instalar o patch macOS em $SITE"

# ---------- 4. Chrome com depuração remota ----------
if [ "${GOTWIC_NO_CHROME:-0}" != "1" ]; then
  if curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
    echo "Chrome já está escutando na porta $PORT."
  else
    echo "Abrindo o Google Chrome com depuração remota na porta $PORT ..."
    mkdir -p "$CHROME_PROFILE"
    # Perfil separado é obrigatório: o Chrome 136+ ignora --remote-debugging-port no perfil padrão.
    # As flags de background evitam que o macOS pause o jogo quando a janela do Chrome fica escondida.
    open -na "Google Chrome" --args \
      --remote-debugging-port="$PORT" \
      --user-data-dir="$CHROME_PROFILE" \
      --disable-background-timer-throttling \
      --disable-backgrounding-occluded-windows \
      --disable-renderer-backgrounding \
      || echo "AVISO: não consegui abrir o Google Chrome. Instale-o em /Applications."
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      curl -s --max-time 1 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1 && break
      sleep 0.5
    done
  fi
fi

# ---------- 5. App ----------
echo "Iniciando GOTWiC Scanner..."
"$PY" startup.py "$@"
STATUS=$?
if [ $STATUS -ne 0 ]; then
  echo
  read -r -p "O app terminou com erro ($STATUS). Pressione Enter para fechar..." _
fi
exit $STATUS
