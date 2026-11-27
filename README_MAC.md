# GOTWiC Scanner no macOS + guia do WASM

Este guia cobre duas coisas:

1. Como rodar o scanner no MacBook (Apple Silicon ou Intel).
2. Como funciona a parte de WASM e o que fazer quando o jogo atualiza e a navegação para de funcionar.

---

## 1. Rodando no Mac

### O que foi adicionado (nenhum arquivo original foi alterado)

| Arquivo | Para quê |
|---|---|
| `RUN_MAC.command` | Equivale ao `RUN_GUI.bat`. Cria o ambiente Python, instala as dependências, abre o Chrome com depuração e inicia o app. |
| `macos_support/gotwic_macos_patch.py` | Faz o ajuste para o Mac em memória: gera o ID do aparelho a partir do UUID de hardware do Mac (no Windows vem do registro) e guarda a licença em `~/Library/Application Support/GOTWiC-Scanner/`. |
| `wasm_tools/wasm_diagnose.py` | Diagnóstico e recuperação da navegação WASM com o jogo aberto. |
| `wasm_tools/wasm_analyze.py` | Análise offline dos binários `.wasm`. Usa só a biblioteca padrão. |

**Por que os originais não foram editados:** o app tem um atualizador obrigatório. Ele compara o SHA-256 de cada arquivo com o GitHub e substitui o que estiver diferente. Se `license_client.py` fosse editado, a próxima atualização apagaria o ajuste. Arquivos que não estão no manifesto nunca são tocados pelo atualizador. Por isso o suporte ao Mac fica em arquivos separados e entra por um arquivo `.pth` dentro do ambiente virtual. Isso vale para todos os processos que o app abre, inclusive o reinício depois de uma atualização.

### Pré-requisitos

- **Python 3.11, 3.12 ou 3.13 com Tkinter.** Recomendado: o instalador do python.org para macOS, que já vem com Tkinter. Pelo Homebrew: `brew install python@3.13 python-tk@3.13`. O atualizador do app usa Tkinter; sem ele, a atualização falha.
- **Google Chrome** em `/Applications`.
- Opcional: MySQL. Sem ele o app funciona normalmente e salva só em CSV. Para usar: `brew install mysql` e configure `GOTWIC_DB_*` como no Windows.

### Primeira execução

1. No Finder, dê dois cliques em `RUN_MAC.command`. Se ele abrir no editor de texto, rode no Terminal: `chmod +x RUN_MAC.command && ./RUN_MAC.command`.
2. Na primeira vez ele cria `.venv-mac/` e instala PySide6 e as outras dependências. Leva alguns minutos.
3. Abre uma janela do Chrome com um perfil separado (`~/Library/Application Support/GOTWiC-Scanner-Chrome`). **Faça login no GTArcade nessa janela** e abra o mapa do jogo. O login fica salvo para as próximas vezes.
4. O app abre, verifica atualizações e pede a licença.

> **Licença:** a chave é vinculada ao aparelho. O Mac tem um ID diferente do PC com Windows. Se a sua chave já foi ativada em um Windows, o servidor vai responder *"This license is already assigned to another computer"*. Nesse caso, peça ao responsável pelas licenças uma chave nova (ou que libere a atual) para o Mac.

### Diferenças em relação ao Windows

- Para parar um scan, o app envia `SIGINT` em vez de `CTRL_BREAK`. O comportamento é o mesmo: o scan termina e salva o CSV.
- O Chrome abre com flags que impedem o macOS de pausar o jogo quando a janela fica escondida atrás do app: `--disable-backgrounding-occluded-windows` e outras. Mesmo assim, evite minimizar o Chrome durante o scan.
- `RUN_GUI.bat` e `CREATE_SHORTCUT.ps1` não são usados no Mac.

---

## 2. Como funciona a parte de WASM

O jogo é um build **Unity WebGL** que roda como WebAssembly dentro do Chrome. O scanner não clica no mapa. Ele:

1. Conecta no Chrome pela porta de depuração (`127.0.0.1:9222`, Chrome DevTools Protocol).
2. Entra no contexto JavaScript do iframe do jogo (`logins-gotpc.gtarcade.com`).
3. Pega a **tabela de funções indiretas** do WASM, `gameInstance.Module.asm.__indirect_function_table`. Ela tem cerca de 64 mil entradas.
4. Chama direto a função interna do jogo que move o mapa:

```
navegação : t.get(63922)  -> função 126674, 5 parâmetros (i32,i32,i32,i32,i32) -> i32
            f(Y*720 + X, 150, 200, 250, 300)  retorna 1 se aceitou
reino     : t.get(63919)  -> função 126705, 6 parâmetros (i64, i32 × 5)
            f(BigInt(2625310000) + reino, 0, 0, 0, 0, 0)
```

5. Escuta os frames do WebSocket do jogo: `9001` traz os castelos/trégua do mapa e `20007` traz o perfil e a força. Esses frames são decodificados como protobuf/LZ4.

O `name` de uma função WASM no JavaScript é o **índice da função** dentro do binário. Por isso o scanner confere `f.name` e `f.length` (número de parâmetros) antes de chamar qualquer coisa.

### Por que quebra a cada atualização

Toda vez que o jogo publica um build novo, o compilador do Unity reordena as funções. A função continua fazendo a mesma coisa, mas muda de **slot** e de **índice**. O preflight do scanner detecta isso e para com `Function identity mismatch`.

Histórico tirado do git deste projeto:

| Data | Navegação (slot / função) | Reino (slot / função) | Diferença de índice |
|---|---|---|---|
| 26/10 | 62958 / 145867 | — | |
| 27/10 | 62960 / 145870 | — | |
| 29/10 | 63014 / 117459 | 63011 / 117486 | +27 |
| 05/11 | 62896 / 117256 | 62893 / 117283 | +27 |
| 14/11 | 63903 / 126651 | 63900 / 126682 | +31 |
| 19/11 | 63919 / 126662 | 63916 / 126693 | +31 |
| 21/11 | 63922 / 126674 | 63919 / 126705 | +31 |

Em todas as versões que tinham os dois, há um padrão estável:

- o seletor de reino fica **exatamente 3 slots antes** da navegação;
- o índice da função de reino é **27 a 31 maior** que o da navegação;
- navegação tem 5 parâmetros e o reino tem 6, sendo o primeiro i64.

As ferramentas usam esse padrão para achar os novos números sem chutar, e confirmam com a análise do binário.

### O que fazer quando o jogo atualizar

Com o Chrome de depuração aberto, logado e **com o mapa visível**, rode no Terminal, na pasta do projeto:

```bash
# 1) Confere se ainda bate (só leitura)
.venv-mac/bin/python wasm_tools/wasm_diagnose.py check

# 2) Se quebrou: faz a recuperação automática (só leitura, não chama função do jogo)
.venv-mac/bin/python wasm_tools/wasm_diagnose.py recover
```

O `recover` executa, em ordem:

1. busca ao vivo na tabela pelos pares que seguem a regra (slot −3, 5/6 parâmetros, índice +20..+40);
2. baixa o `.wasm` atual para `wasm_builds/`;
3. analisa o binário offline com os **tipos exatos** dos parâmetros;
4. se existir uma referência salva em `wasm_reference/`, compara o código das funções (opcodes, chamadas, constantes e bytes);
5. mostra o candidato recomendado e os **comandos exatos** para os próximos passos.

Depois, sempre um passo por vez:

```bash
# 3) UMA chamada controlada: o mapa deve ir para X=125 Y=325 (água)
.venv-mac/bin/python wasm_tools/wasm_diagnose.py test --slot <S> --name <N>
#    -> retorno 1 + frames 9001 + você VÊ o mapa mexer

# (opcional) testar o seletor de reino
.venv-mac/bin/python wasm_tools/wasm_diagnose.py test-kingdom --slot <S-3> --name <K> --kingdom 542

# 4) Gravar no scanner (faz backup .bak-<data> automaticamente)
.venv-mac/bin/python wasm_tools/wasm_diagnose.py apply --nav-slot <S> --nav-name <N> --kingdom-slot <S-3> --kingdom-name <K>

# 5) Conferir e rodar um scan normal pelo app
.venv-mac/bin/python wasm_tools/wasm_diagnose.py check

# 6) Com o scan funcionando de ponta a ponta, salve a referência desta versão
.venv-mac/bin/python wasm_tools/wasm_diagnose.py save-reference
```

**Importante: rode `save-reference` agora**, enquanto a versão atual funciona. Ele guarda um arquivo pequeno (`wasm_reference/reference_*.json`) com o "DNA" das duas funções verificadas. Na próxima atualização, o `recover` compara o build novo com essa referência. É a confirmação mais forte que existe, e não exige guardar o binário antigo.

Se a sua versão atual já está quebrada e não há referência, o `recover` usa só a regra estrutural mais o tamanho da função (a navegação verificada tinha cerca de 575 bytes). Ela costuma bastar, mas o `test` com confirmação visual é obrigatório antes do `apply`.

### Ferramentas offline (sem o jogo aberto)

```bash
python3 wasm_tools/wasm_analyze.py info  wasm_builds/gotwic_<sha>.wasm
python3 wasm_tools/wasm_analyze.py slot  wasm_builds/gotwic_<sha>.wasm 63922 63919
python3 wasm_tools/wasm_analyze.py pairs wasm_builds/gotwic_<sha>.wasm --near 63922
python3 wasm_tools/wasm_analyze.py match wasm_builds/gotwic_<novo>.wasm --ref wasm_reference/reference_<sha>.json
```

### Quando não for só trocar números

O `check` avisa nestes casos, e aí é preciso ajustar o código do scanner, não só os números:

- **A tabela não está mais em `gameInstance.Module.asm.__indirect_function_table`:** o Unity ou o loader mudou de versão.
- **O contexto `logins-gotpc.gtarcade.com` não aparece:** o jogo mudou de domínio. O `check` lista as origens encontradas.
- **Nenhum par segue a regra:** a estrutura do código mudou. Compare manualmente os candidatos com `wasm_analyze.py match` e teste um por vez.
- **A navegação responde 1 e o mapa se move, mas os castelos não aparecem:** o problema está no protocolo `9001`, não no WASM.

Também pode haver mudança nos argumentos fixos (`150,200,250,300`) ou no código do reino (`2625310000`). Só altere esses valores com prova independente, como diz o `WASM_RECOVERY_GUIDELINE.md`.

---

## Problemas comuns

| Sintoma | Solução |
|---|---|
| `Chrome não responde em 127.0.0.1:9222` | Feche todas as janelas do Chrome e abra pelo `RUN_MAC.command`. O Chrome 136+ só aceita depuração com perfil separado, e o script já faz isso. |
| `Aba do GTArcade não encontrada` | Abra o jogo **na janela que o script abriu**, não no seu Chrome normal. |
| `Cannot identify this computer` | O app foi iniciado sem passar pelo `RUN_MAC.command`, então o patch não foi carregado. Abra pelo `.command`. |
| A atualização falha ou o app fecha logo depois de aceitar a atualização | Falta Tkinter no Python. Rode `brew install python-tk@3.13` (ou use o Python do python.org), apague `.venv-mac/` e rode de novo. |
| O binário veio comprimido (Brotli) | `.venv-mac/bin/python -m pip install brotli` e repita o comando. |
| `recover` não acha o binário | Copie a URL do `.wasm` no DevTools (aba Network) e passe com `--wasm-url <url>`. |

---

## 3. Atualização do jogo de 25/11/2026: o que mudou e como ficou

- **O jogo passou a mandar os dados em JSON** em vez de protobuf. O scanner agora lê os dois formatos:
  - mapa/castelos (`9001`, em `live_truce_scanner_v5.py`);
  - perfil do castelo com poder, eliminações e mérito (`20007`, em `gotwic_scanner_v2_2.py`).
- **Força pela classificação:** no início de cada scan, logo depois de trocar de reino, o scanner pede ao jogo a Classificação de Poder do reino, sem abrir a janela. Ela traz os 100 mais fortes e fica salva em `ranking/power_ranking.json` por 6 horas. Depois da varredura, quem está na classificação recebe a força na hora. Com *Castle power (verbose)*, só os demais são visitados um a um.
- Se o pedido falhar, aparece `Power ranking request skipped: …` e o scan continua normalmente. A causa é sempre uma atualização do jogo.

### Recuperar o pedido da classificação depois de uma atualização do jogo

Os números ficam em `wasm_reference/rank_request.json`. Para descobrir os novos (jogo aberto no mapa):

1. `wasm_diagnose.py dump`: baixa o binário novo.
2. `trace_requests.py --seconds 40`: abra a Classificação de Poder. A primeira função da pilha logo acima da camada de rede, na resposta `20403`, é a função do pedido. Anote o número dela e o slot.
3. `capture_args.py <função>`: abra a classificação de novo. Anote os valores (objeto, 1, 1, method).
4. `find_root.py search --this <objeto> --method <method>`: procura os caminhos fixos na memória.
5. Recarregue a página e rode `find_root.py resolve`. Depois rode `capture_args.py` outra vez e confirme que o caminho com `root_is_klass: True` e o caminho do method dão os valores novos.
6. Atualize `wasm_reference/rank_request.json` com slot, função, `object_root`, `object_offsets` e `method_root`.
7. Teste com `rank_request.py call`, que deve mostrar "Classificação recebida".
