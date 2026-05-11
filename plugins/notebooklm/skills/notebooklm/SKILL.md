---
name: notebooklm
description: API completa para o Google NotebookLM - acesso programático total incluindo funcionalidades não disponíveis na interface web. Crie cadernos, adicione fontes, gere todos os tipos de artefatos, baixe em múltiplos formatos. Ativa com /notebooklm explícito ou intenções como "criar um podcast sobre X"
---

# Automação do NotebookLM

Acesso programático completo ao Google NotebookLM — incluindo funcionalidades não expostas na interface web. Crie cadernos, adicione fontes (URLs, YouTube, PDFs, áudio, vídeo, imagens), converse com o conteúdo, gere todos os tipos de artefatos e baixe os resultados em múltiplos formatos.

## Instalação

**Via PyPI (Recomendado):**
```bash
pip install notebooklm-py
```

**Via GitHub (use a tag da versão mais recente, NÃO o branch main):**
```bash
# Obter a tag da versão mais recente (usando curl)
LATEST_TAG=$(curl -s https://api.github.com/repos/teng-lin/notebooklm-py/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
pip install "git+https://github.com/teng-lin/notebooklm-py@${LATEST_TAG}"
```

⚠️ **NÃO instale pelo branch main** (`pip install git+https://github.com/teng-lin/notebooklm-py`). O branch main pode conter alterações não lançadas/instáveis. Sempre use o PyPI ou uma tag de versão específica, a menos que esteja testando funcionalidades não lançadas.

**Métodos de instalação da skill:**

- `notebooklm skill install` instala esta skill nos diretórios de agentes locais suportados, gerenciados pela CLI.
- `npx skills add teng-lin/notebooklm-py` instala esta skill a partir do repositório GitHub nos diretórios de skills de agentes compatíveis.
- Se você já estiver lendo este arquivo dentro de um diretório de skills de agente, a skill já está instalada. Você só precisa do pacote Python e da autenticação abaixo.

**Instalação gerenciada pela CLI:**
```bash
notebooklm skill install
```

## Pré-requisitos

**IMPORTANTE:** Antes de usar qualquer comando, você DEVE se autenticar:

```bash
notebooklm login          # Abre o navegador para OAuth do Google
notebooklm list           # Verifica se a autenticação funciona
```

Se os comandos falharem com erros de autenticação, execute novamente `notebooklm login`.

### CI/CD, Múltiplas Contas e Agentes Paralelos

Para ambientes automatizados, múltiplas contas ou fluxos de agentes paralelos:

| Variável | Finalidade |
|----------|-----------|
| `NOTEBOOKLM_HOME` | Diretório de configuração personalizado (padrão: `~/.notebooklm`) |
| `NOTEBOOKLM_PROFILE` | Nome do perfil ativo (padrão: `default`) |
| `NOTEBOOKLM_AUTH_JSON` | JSON de autenticação inline — sem necessidade de gravações em arquivo |

**Configuração CI/CD:** Defina `NOTEBOOKLM_AUTH_JSON` a partir de um segredo contendo o conteúdo do seu `storage_state.json`.

**Múltiplas contas:** Use perfis nomeados (`notebooklm profile create work`, depois `notebooklm -p work login`). Alternativamente, use diretórios `NOTEBOOKLM_HOME` diferentes por conta.

**Agentes paralelos:** A CLI armazena o contexto do caderno por perfil (`~/.notebooklm/profiles/<profile>/context.json`, com fallback legado para `~/.notebooklm/context.json` para o perfil padrão implícito). Múltiplos agentes simultâneos que compartilham um perfil e usam `notebooklm use` podem sobrescrever o contexto uns dos outros — use uma das estratégias de isolamento abaixo.

**Soluções para fluxos paralelos:**
1. **Sempre use ID de caderno explícito** (recomendado): Passe `-n <notebook_id>` (para comandos `wait`/`download`) ou `--notebook <notebook_id>` (para outros) em vez de depender de `use`
2. **Isolamento por agente via perfis:** `export NOTEBOOKLM_PROFILE=agent-$ID` (cada perfil tem seu próprio arquivo de contexto)
3. **Isolamento por agente via home:** Defina `NOTEBOOKLM_HOME` único por agente: `export NOTEBOOKLM_HOME=/tmp/agent-$ID`
4. **Use UUIDs completos:** Evite IDs parciais em automação (podem se tornar ambíguos)

## Verificação de Configuração do Agente

Antes de iniciar fluxos de trabalho, verifique se a CLI está pronta:

1. `notebooklm status` → Deve mostrar "Authenticated as: email@..."
2. `notebooklm list --json` → Deve retornar JSON válido (mesmo que a lista de cadernos esteja vazia)
3. Se algum falhar → Execute `notebooklm login`

## Quando Esta Skill É Ativada

**Explícito:** Usuário diz "/notebooklm", "use notebooklm", ou menciona a ferramenta pelo nome

**Detecção de intenção:** Reconheça solicitações como:
- "Crie um podcast sobre [tópico]"
- "Resuma estas URLs/documentos"
- "Gere um quiz a partir da minha pesquisa"
- "Transforme isso em uma visão geral em áudio"
- "Crie flashcards para estudar"
- "Gere um vídeo explicativo"
- "Faça um infográfico"
- "Crie um mapa mental dos conceitos"
- "Baixe o quiz em markdown"
- "Adicione estas fontes ao NotebookLM"

## Regras de Autonomia

**Execute automaticamente (sem confirmação):**
- `notebooklm status` - verificar contexto
- `notebooklm auth check` - diagnosticar problemas de autenticação
- `notebooklm list` - listar cadernos
- `notebooklm source list` - listar fontes
- `notebooklm artifact list` - listar artefatos
- `notebooklm language list` - listar idiomas suportados
- `notebooklm language get` - obter idioma atual
- `notebooklm language set` - definir idioma (configuração global)
- `notebooklm artifact wait` - aguardar conclusão do artefato (em contexto de subagente)
- `notebooklm source wait` - aguardar processamento da fonte (em contexto de subagente)
- `notebooklm research status` - verificar status da pesquisa
- `notebooklm research wait` - aguardar pesquisa (em contexto de subagente)
- `notebooklm use <id>` - definir contexto (⚠️ SOMENTE AGENTE ÚNICO - use flag `-n` em fluxos paralelos)
- `notebooklm create` - criar caderno
- `notebooklm ask "..."` - consultas de chat (sem `--save-as-note`)
- `notebooklm history` - exibir histórico de conversa (somente leitura)
- `notebooklm source add` - adicionar fontes
- `notebooklm profile list` - listar perfis
- `notebooklm profile create` - criar perfil
- `notebooklm profile switch` - alternar perfil ativo
- `notebooklm doctor` - verificar saúde do ambiente

**Perguntar antes de executar:**
- `notebooklm delete` - destrutivo
- `notebooklm generate *` - longa duração, pode falhar
- `notebooklm download *` - grava no sistema de arquivos
- `notebooklm artifact wait` - longa duração (quando na conversa principal)
- `notebooklm source wait` - longa duração (quando na conversa principal)
- `notebooklm research wait` - longa duração (quando na conversa principal)
- `notebooklm ask "..." --save-as-note` - grava uma nota
- `notebooklm history --save` - grava uma nota

## Referência Rápida

| Tarefa | Comando |
|--------|---------|
| Autenticar | `notebooklm login` |
| Diagnosticar problemas de autenticação | `notebooklm auth check` |
| Diagnóstico completo de autenticação | `notebooklm auth check --test` |
| Listar cadernos | `notebooklm list` |
| Criar caderno | `notebooklm create "Título"` |
| Definir contexto | `notebooklm use <notebook_id>` |
| Mostrar contexto | `notebooklm status` |
| Adicionar fonte URL | `notebooklm source add "https://..."` |
| Adicionar arquivo | `notebooklm source add ./arquivo.pdf` |
| Adicionar YouTube | `notebooklm source add "https://youtube.com/..."` |
| Listar fontes | `notebooklm source list` |
| Excluir fonte por ID | `notebooklm source delete <source_id>` |
| Excluir fonte por título exato | `notebooklm source delete-by-title "Título Exato"` |
| Aguardar processamento da fonte | `notebooklm source wait <source_id>` |
| Pesquisa web (rápida) | `notebooklm source add-research "consulta"` |
| Pesquisa web (profunda) | `notebooklm source add-research "consulta" --mode deep --no-wait` |
| Verificar status da pesquisa | `notebooklm research status` |
| Aguardar pesquisa | `notebooklm research wait --import-all` |
| Chat | `notebooklm ask "pergunta"` |
| Chat (fontes específicas) | `notebooklm ask "pergunta" -s src_id1 -s src_id2` |
| Chat (com referências) | `notebooklm ask "pergunta" --json` |
| Chat (salvar resposta como nota) | `notebooklm ask "pergunta" --save-as-note` |
| Chat (salvar com título) | `notebooklm ask "pergunta" --save-as-note --note-title "Título"` |
| Mostrar histórico de conversa | `notebooklm history` |
| Salvar todo histórico como nota | `notebooklm history --save` |
| Continuar conversa específica | `notebooklm ask "pergunta" -c <conversation_id>` |
| Salvar histórico com título | `notebooklm history --save --note-title "Minha Pesquisa"` |
| Obter texto completo da fonte | `notebooklm source fulltext <source_id>` |
| Obter guia da fonte | `notebooklm source guide <source_id>` |
| Gerar podcast | `notebooklm generate audio "instruções"` |
| Gerar podcast (JSON) | `notebooklm generate audio --json` |
| Gerar podcast (fontes específicas) | `notebooklm generate audio -s src_id1 -s src_id2` |
| Gerar vídeo | `notebooklm generate video "instruções"` |
| Gerar relatório | `notebooklm generate report --format briefing-doc` |
| Gerar relatório (instruções adicionais) | `notebooklm generate report --format study-guide --append "Público-alvo: iniciantes"` |
| Gerar quiz | `notebooklm generate quiz` |
| Revisar slide | `notebooklm generate revise-slide "prompt" --artifact <id> --slide 0` |
| Verificar status do artefato | `notebooklm artifact list` |
| Aguardar conclusão | `notebooklm artifact wait <artifact_id>` |
| Baixar áudio | `notebooklm download audio ./saida.mp3` |
| Baixar vídeo | `notebooklm download video ./saida.mp4` |
| Baixar apresentação (PDF) | `notebooklm download slide-deck ./slides.pdf` |
| Baixar apresentação (PPTX) | `notebooklm download slide-deck ./slides.pptx --format pptx` |
| Baixar relatório | `notebooklm download report ./relatorio.md` |
| Baixar mapa mental | `notebooklm download mind-map ./mapa.json` |
| Baixar tabela de dados | `notebooklm download data-table ./dados.csv` |
| Baixar quiz | `notebooklm download quiz quiz.json` |
| Baixar quiz (markdown) | `notebooklm download quiz --format markdown quiz.md` |
| Baixar flashcards | `notebooklm download flashcards cards.json` |
| Baixar flashcards (markdown) | `notebooklm download flashcards --format markdown cards.md` |
| Excluir caderno | `notebooklm notebook delete <id>` |
| Listar idiomas | `notebooklm language list` |
| Obter idioma | `notebooklm language get` |
| Definir idioma | `notebooklm language set pt_BR` |
| Listar perfis | `notebooklm profile list` |
| Criar perfil | `notebooklm profile create trabalho` |
| Alternar perfil | `notebooklm profile switch trabalho` |
| Excluir perfil | `notebooklm profile delete antigo` |
| Renomear perfil | `notebooklm profile rename antigo novo` |
| Usar perfil (pontual) | `notebooklm -p trabalho list` |
| Verificação de saúde | `notebooklm doctor` |
| Verificação de saúde (autocorreção) | `notebooklm doctor --fix` |

**Segurança paralela:** Use IDs de caderno explícitos em fluxos paralelos. Comandos que suportam atalho `-n`: `artifact wait`, `source wait`, `research wait/status`, `download *`. Comandos de download também suportam `-a/--artifact`. Outros comandos usam `--notebook`. Para chat, use `-c <conversation_id>` para direcionar a uma conversa específica.

**IDs parciais:** Use os primeiros 6+ caracteres dos UUIDs. Deve ser prefixo único (falha se ambíguo). Funciona para comandos baseados em ID como `use`, `source delete` e `wait`. Para exclusão por título exato de fonte, use `source delete-by-title "Título"`. Em automação, prefira UUIDs completos para evitar ambiguidade.

## Formatos de Saída dos Comandos

Comandos com `--json` retornam dados estruturados para análise:

**Criar caderno:**
```bash
$ notebooklm create "Pesquisa" --json
{"notebook": {"id": "abc123de-...", "title": "Pesquisa", "created_at": null}}
# analisar com: jq -r .notebook.id
```

**Adicionar fonte:**
```bash
$ notebooklm source add "https://exemplo.com" --json
{"source": {"id": "def456...", "title": "Exemplo", "type": "SourceType.WEB_PAGE", "url": "https://exemplo.com"}}
# analisar com: jq -r .source.id
# Nota: sem campo `status` ao adicionar — use `source list --json` ou `source wait` para verificar o estado de processamento.
```

**Gerar artefato:**
```bash
$ notebooklm generate audio "Foco nos pontos principais" --json
{"task_id": "xyz789...", "status": "pending"}
# Quando executado com --wait, o status concluído também inclui um campo `url`.
```

**Chat com referências:**
```bash
$ notebooklm ask "O que é X?" --json
{"answer": "X é... [1] [2]", "conversation_id": "...", "turn_number": 1, "is_follow_up": false, "references": [{"source_id": "abc123...", "citation_number": 1, "cited_text": "Trecho relevante da fonte..."}, {"source_id": "def456...", "citation_number": 2, "cited_text": "Outro trecho..."}]}
```

**Texto completo da fonte (obter conteúdo indexado):**
```bash
$ notebooklm source fulltext <source_id> --json
{"source_id": "...", "title": "...", "content": "Texto indexado completo...", "_type_code": null, "url": null, "char_count": 12345}
```

**Entendendo citações:** O `cited_text` nas referências frequentemente é um trecho ou cabeçalho de seção, não a passagem completa citada. As posições `start_char`/`end_char` referenciam o índice interno fragmentado do NotebookLM, não o texto bruto completo. Use `SourceFulltext.find_citation_context()` para localizar citações:
```python
fulltext = await client.sources.get_fulltext(notebook_id, ref.source_id)
matches = fulltext.find_citation_context(ref.cited_text)  # Retorna list[(context, position)]
if matches:
    context, pos = matches[0]  # Primeira correspondência; verifique len(matches) > 1 para duplicatas
```

**Extraindo IDs:** Endpoints singulares envolvem seu resultado em um envelope —
analise `.notebook.id` (de `create`), `.source.id` (de `source add`),
ou `.task_id` (de `generate *`). A lista de referências do chat `--json` usa
`.references[].source_id`.

## Tipos de Geração

Todos os comandos de geração suportam:
- `-s, --source` para usar fonte(s) específica(s) em vez de todas as fontes
- `--language` para definir o idioma de saída (padrão: idioma configurado ou 'en')
- `--json` para saída legível por máquina (retorna `task_id` e `status`)
- `--retry N` para tentar automaticamente novamente em limites de taxa com backoff exponencial

| Tipo | Comando | Opções | Download |
|------|---------|--------|----------|
| Podcast | `generate audio` | `--format [deep-dive\|brief\|critique\|debate]`, `--length [short\|default\|long]` | .mp3 |
| Vídeo | `generate video` | `--format [explainer\|brief]`, `--style [auto\|classic\|whiteboard\|kawaii\|anime\|watercolor\|retro-print\|heritage\|paper-craft]` | .mp4 |
| Apresentação | `generate slide-deck` | `--format [detailed\|presenter]`, `--length [default\|short]` | .pdf / .pptx |
| Revisão de Slide | `generate revise-slide "prompt" --artifact <id> --slide N` | `--wait`, `--notebook` | *(re-baixa o deck pai)* |
| Infográfico | `generate infographic` | `--orientation [landscape\|portrait\|square]`, `--detail [concise\|standard\|detailed]`, `--style [auto\|sketch-note\|professional\|bento-grid\|editorial\|instructional\|bricks\|clay\|anime\|kawaii\|scientific]` | .png |
| Relatório | `generate report` | `--format [briefing-doc\|study-guide\|blog-post\|custom]`, `--append "instruções extras"` (¹) | .md |
| Mapa Mental | `generate mind-map` | *(síncrono, instantâneo)* | .json |
| Tabela de Dados | `generate data-table` | descrição obrigatória | .csv |
| Quiz | `generate quiz` | `--difficulty [easy\|medium\|hard]`, `--quantity [fewer\|standard\|more]` | .json/.md/.html |
| Flashcards | `generate flashcards` | `--difficulty [easy\|medium\|hard]`, `--quantity [fewer\|standard\|more]` | .json/.md/.html |

¹ `--append` apenas personaliza os modelos integrados. Com `--format custom`, passe o prompt como argumento posicional `DESCRIPTION` (`notebooklm generate report "PROMPT" --format custom`); `--append` é silenciosamente ignorado nesse modo (a CLI exibe um aviso).

## Funcionalidades Além da Interface Web

Estas capacidades estão disponíveis via CLI mas não na interface web do NotebookLM:

| Funcionalidade | Comando | Descrição |
|----------------|---------|-----------|
| **Downloads em lote** | `download <type> --all` | Baixa todos os artefatos de um tipo de uma vez |
| **Exportação de Quiz/Flashcard** | `download quiz --format json` | Exporta como JSON, Markdown ou HTML (a interface web só mostra visualização interativa) |
| **Extração de mapa mental** | `download mind-map` | Exporta JSON hierárquico para ferramentas de visualização |
| **Exportação de tabela de dados** | `download data-table` | Baixa tabelas estruturadas como CSV |
| **Apresentação como PPTX** | `download slide-deck --format pptx` | Baixa apresentação como .pptx editável (a interface web só oferece PDF) |
| **Revisão de slide** | `generate revise-slide "prompt" --artifact <id> --slide N` | Modifica slides individuais com prompt em linguagem natural |
| **Adição a modelo de relatório** | `generate report --format study-guide --append "..."` | Acrescenta instruções personalizadas a modelos de formato integrados sem perder o tipo de formato |
| **Texto completo da fonte** | `source fulltext <id>` | Recupera o conteúdo de texto indexado de qualquer fonte |
| **Salvar chat como nota** | `ask "..." --save-as-note` / `history --save` | Salva respostas de P&R ou histórico de conversa como notas do caderno |
| **Compartilhamento programático** | comandos `share` | Gerencia permissões de compartilhamento sem a interface |

## Fluxos de Trabalho Comuns

### Pesquisa para Podcast (Interativo)
**Tempo:** 5 a 10 minutos no total

1. `notebooklm create "Pesquisa: [tópico]"` — *se falhar: verifique autenticação com `notebooklm login`*
2. `notebooklm source add` para cada URL/documento — *se um falhar: registre aviso, continue com os outros*
3. Aguarde as fontes: `notebooklm source list --json` até que todas tenham status=READY — *necessário antes da geração*
4. `notebooklm generate audio "Foque em [ângulo específico]"` (confirme quando solicitado) — *se limite de taxa: aguarde 5 min, tente novamente uma vez*
5. Anote o ID do artefato retornado
6. Verifique `notebooklm artifact list` mais tarde para o status
7. `notebooklm download audio ./podcast.mp3` quando concluído (confirme quando solicitado)

### Pesquisa para Podcast (Automatizado com Subagente)
**Tempo:** 5 a 10 minutos, mas continua em segundo plano

Quando o usuário quiser automação completa (gerar e baixar quando pronto):

1. Crie o caderno e adicione fontes normalmente
2. Aguarde as fontes ficarem prontas (use `source wait` ou verifique `source list --json`)
3. Execute `notebooklm generate audio "..." --json` → analise `artifact_id` da saída
4. **Inicie um agente em segundo plano** usando a ferramenta Task:
   ```
   Task(
     prompt="Aguarde o artefato {artifact_id} no caderno {notebook_id} ser concluído, depois baixe.
             Use: notebooklm artifact wait {artifact_id} -n {notebook_id} --timeout 600
             Então: notebooklm download audio ./podcast.mp3 -a {artifact_id} -n {notebook_id}",
     subagent_type="general-purpose"
   )
   ```
5. A conversa principal continua enquanto o agente aguarda

**Tratamento de erros no subagente:**
- Se `artifact wait` retornar código de saída 2 (timeout): Reporte o timeout, sugira verificar `artifact list`
- Se o download falhar: Verifique se o status do artefato é COMPLETED primeiro

**Benefícios:** Não bloqueante, o usuário pode fazer outras tarefas, download automático na conclusão

### Análise de Documentos
**Tempo:** 1 a 2 minutos

1. `notebooklm create "Análise: [projeto]"`
2. `notebooklm source add ./doc.pdf` (ou URLs)
3. `notebooklm ask "Resuma os pontos principais"`
4. `notebooklm ask "Quais são os argumentos centrais?"`
5. Continue conversando conforme necessário

### Importação em Lote
**Tempo:** Varia conforme o número de fontes

1. `notebooklm create "Coleção: [nome]"`
2. Adicione múltiplas fontes:
   ```bash
   notebooklm source add "https://url1.com"
   notebooklm source add "https://url2.com"
   notebooklm source add ./arquivo-local.pdf
   ```
3. `notebooklm source list` para verificar

**Limites de fontes:** Varia por plano — Standard: 50, Plus: 100, Pro: 300, Ultra: 600 fontes por caderno. Consulte os [planos do NotebookLM](https://support.google.com/notebooklm/answer/16213268) para detalhes. A CLI não aplica esses limites; eles são aplicados pela sua conta do NotebookLM.
**Tipos suportados:** PDFs, URLs do YouTube, URLs da web, Google Docs, arquivos de texto, Markdown, documentos Word, EPUB, arquivos de áudio, arquivos de vídeo, imagens

### Importação em Lote com Espera de Fontes (Padrão Subagente)
**Tempo:** Varia conforme o número de fontes

Quando adicionar múltiplas fontes e precisar aguardar o processamento antes de chat/geração:

1. Adicione fontes com `--json` para capturar IDs (analise com `jq -r .source.id`):
   ```bash
   notebooklm source add "https://url1.com" --json  # → {"source": {"id": "abc...", ...}}
   notebooklm source add "https://url2.com" --json  # → {"source": {"id": "def...", ...}}
   ```
2. **Inicie um agente em segundo plano** para aguardar todas as fontes:
   ```
   Task(
     prompt="Aguarde as fontes {source_ids} no caderno {notebook_id} ficarem prontas.
             Para cada uma: notebooklm source wait {id} -n {notebook_id} --timeout 120
             Reporte quando todas estiverem prontas ou se alguma falhar.",
     subagent_type="general-purpose"
   )
   ```
3. A conversa principal continua enquanto o agente aguarda
4. Assim que as fontes estiverem prontas, prossiga com chat ou geração

**Por que aguardar as fontes?** As fontes devem ser indexadas antes do chat ou geração. Leva de 10 a 60 segundos por fonte.

### Pesquisa Web Profunda (Padrão Subagente)
**Tempo:** 2 a 5 minutos, executa em segundo plano

A pesquisa profunda encontra e analisa fontes web sobre um tópico:

1. Crie caderno: `notebooklm create "Pesquisa: [tópico]"`
2. Inicie pesquisa profunda (não bloqueante):
   ```bash
   notebooklm source add-research "consulta do tópico" --mode deep --no-wait
   ```
3. **Inicie um agente em segundo plano** para aguardar e importar:
   ```
   Task(
     prompt="Aguarde a pesquisa no caderno {notebook_id} ser concluída e importe as fontes.
             Use: notebooklm research wait -n {notebook_id} --import-all --timeout 300
             Reporte quantas fontes foram importadas.",
     subagent_type="general-purpose"
   )
   ```
4. A conversa principal continua enquanto o agente aguarda
5. Quando o agente concluir, as fontes são importadas automaticamente

**Alternativa (bloqueante):** Para casos simples, omita `--no-wait`:
```bash
notebooklm source add-research "tópico" --mode deep --import-all
# Bloqueia por até 5 minutos
```

**Quando usar cada modo:**
- `--mode fast`: Tópico específico, visão geral rápida necessária (5 a 10 fontes, segundos)
- `--mode deep`: Tópico amplo, análise abrangente necessária (20+ fontes, 2 a 5 min)

**Fontes de pesquisa:**
- `--from web`: Pesquisa na web (padrão)
- `--from drive`: Pesquisa no Google Drive

## Estilo de Saída

**Atualizações de progresso:** Status breve para cada etapa
- "Criando caderno 'Pesquisa: IA'..."
- "Adicionando fonte: https://exemplo.com..."
- "Iniciando geração de áudio... (ID da tarefa: abc123)"

**Fire-and-forget para operações longas:**
- Inicie a geração, retorne o ID do artefato imediatamente
- NÃO faça polling ou aguarde na conversa principal — a geração leva de 5 a 45 minutos (veja a tabela de tempos)
- O usuário verifica o status manualmente, OU use subagente com `artifact wait`

**Saída JSON:** Use a flag `--json` para saída legível por máquina:
```bash
notebooklm list --json
notebooklm auth check --json
notebooklm source list --json
notebooklm artifact list --json
```

**Esquemas JSON (campos principais):**

`notebooklm list --json`:
```json
{"notebooks": [{"index": 1, "id": "...", "title": "...", "is_owner": true, "created_at": "..."}], "count": 1}
```

`notebooklm auth check --json`:
```json
{"status": "ok", "checks": {"storage_exists": true, "json_valid": true, "cookies_present": true, "sid_cookie": true, "token_fetch": true}, "details": {"storage_path": "...", "auth_source": "file", "cookies_found": ["SID", "HSID", "..."], "cookie_domains": [".google.com"]}}
```

`notebooklm source list --json`:
```json
{"notebook_id": "...", "notebook_title": "...", "sources": [{"index": 1, "id": "...", "title": "...", "type": "SourceType.WEB_PAGE", "url": "...", "status": "ready|processing|error", "status_id": 1, "created_at": "..."}], "count": 1}
```

`notebooklm artifact list --json`:
```json
{"notebook_id": "...", "notebook_title": "...", "artifacts": [{"index": 1, "id": "...", "title": "...", "type": "Audio", "type_id": 1, "status": "in_progress|pending|completed|unknown", "status_id": 1, "created_at": "..."}], "count": 1}
```

**Valores de status:**
- Fontes: `processing` → `ready` (ou `error`)
- Artefatos: `pending` ou `in_progress` → `completed` (ou `unknown`)

## Tratamento de Erros

**Em caso de falha, ofereça ao usuário uma escolha:**
1. Tentar novamente a operação
2. Pular e continuar com outra coisa
3. Investigar o erro

**Árvore de decisão de erros:**

| Erro | Causa | Ação |
|------|-------|------|
| Erro de autenticação/cookie | Sessão expirada | Execute `notebooklm auth check` depois `notebooklm login` |
| "No notebook context" | Contexto não definido | Use flag `-n <id>` ou `--notebook <id>` (paralelo), ou `notebooklm use <id>` (agente único) |
| "No result found for RPC ID" | Limite de taxa | Aguarde 5 a 10 min, tente novamente |
| `GENERATION_FAILED` | Limite de taxa do Google | Aguarde e tente novamente mais tarde |
| Download falha | Geração incompleta | Verifique `artifact list` para o status |
| ID de caderno/fonte inválido | ID errado | Execute `notebooklm list` para verificar |
| Erro de protocolo RPC | Google alterou APIs | Pode precisar atualizar a CLI |

## Códigos de Saída

Todos os comandos usam códigos de saída consistentes:

| Código | Significado | Ação |
|--------|-------------|------|
| 0 | Sucesso | Continue |
| 1 | Erro (não encontrado, processamento falhou) | Verifique stderr, veja Tratamento de Erros |
| 2 | Timeout (apenas comandos wait) | Aumente o timeout ou verifique o status manualmente |

**Exemplos:**
- `source wait` retorna 1 se a fonte não for encontrada ou o processamento falhar
- `artifact wait` retorna 2 se o timeout for atingido antes da conclusão
- `generate` retorna 1 se o limite de taxa for atingido (verifique stderr para detalhes)

## Limitações Conhecidas

**Limite de taxa:** Geração de áudio, vídeo, quiz, flashcards, infográfico e apresentação pode falhar devido aos limites de taxa do Google. Esta é uma limitação da API, não um bug.

**Operações confiáveis:** Estas sempre funcionam:
- Cadernos (listar, criar, excluir, renomear)
- Fontes (adicionar, listar, excluir)
- Chat/consultas
- Geração de mapa mental, guia de estudos, relatório, tabela de dados

**Operações não confiáveis:** Estas podem falhar com limite de taxa:
- Geração de áudio (podcast)
- Geração de vídeo
- Geração de quiz e flashcard
- Geração de infográfico e apresentação

**Solução alternativa:** Se a geração falhar:
1. Verifique o status: `notebooklm artifact list`
2. Tente novamente após 5 a 10 minutos
3. Use a interface web do NotebookLM como alternativa

**Os tempos de processamento variam significativamente.** Use o padrão de subagente para operações longas:

| Operação | Tempo típico | Timeout sugerido |
|----------|--------------|-----------------|
| Processamento de fonte | 30s a 10 min | 600s |
| Pesquisa (rápida) | 30s a 2 min | 180s |
| Pesquisa (profunda) | 15 a 30+ min | 1800s |
| Notas | instantâneo | n/a |
| Mapa mental | instantâneo (síncrono) | n/a |
| Quiz, flashcards | 5 a 15 min | 900s |
| Relatório, tabela de dados | 5 a 15 min | 900s |
| Geração de áudio | 10 a 20 min | 1200s |
| Geração de vídeo | 15 a 45 min | 2700s |

**Intervalos de polling:** Ao verificar o status manualmente, faça polling a cada 15 a 30 segundos para evitar chamadas excessivas à API.

## Configuração de Idioma

A configuração de idioma controla o idioma de saída para artefatos gerados (áudio, vídeo, etc.).

**Importante:** O idioma é uma configuração **GLOBAL** que afeta todos os cadernos da sua conta.

```bash
# Listar todos os 80+ idiomas suportados com nomes nativos
notebooklm language list

# Mostrar configuração de idioma atual
notebooklm language get

# Definir idioma para geração de artefatos
notebooklm language set pt_BR   # Português (Brasil)
notebooklm language set zh_Hans # Chinês Simplificado
notebooklm language set en      # Inglês (padrão)
```

**Códigos de idioma comuns:**
| Código | Idioma |
|--------|--------|
| `en` | English |
| `pt_BR` | Português (Brasil) |
| `zh_Hans` | 中文（简体） - Chinês Simplificado |
| `zh_Hant` | 中文（繁體） - Chinês Tradicional |
| `ja` | 日本語 - Japonês |
| `ko` | 한국어 - Coreano |
| `es` | Español - Espanhol |
| `fr` | Français - Francês |
| `de` | Deutsch - Alemão |

**Substituição por comando:** Use a flag `--language` nos comandos de geração:
```bash
notebooklm generate audio --language pt_BR  # Podcast em português
notebooklm generate video --language ja     # Vídeo em japonês
```

**Modo offline:** Use a flag `--local` para pular a sincronização com o servidor:
```bash
notebooklm language set pt_BR --local  # Salva somente localmente
notebooklm language get --local        # Lê configuração local apenas
```

## Solução de Problemas

```bash
notebooklm --help              # Comandos principais
notebooklm auth check          # Diagnosticar problemas de autenticação
notebooklm auth check --test   # Validação completa de autenticação com teste de rede
notebooklm notebook --help     # Gerenciamento de cadernos
notebooklm source --help       # Gerenciamento de fontes
notebooklm research --help     # Status/espera de pesquisa
notebooklm generate --help     # Geração de conteúdo
notebooklm artifact --help     # Gerenciamento de artefatos
notebooklm download --help     # Download de conteúdo
notebooklm language --help     # Configurações de idioma
```

**Diagnosticar autenticação:** `notebooklm auth check` — mostra domínios de cookies, caminho de armazenamento, status de validação
**Reautenticar:** `notebooklm login`
**Verificar versão:** `notebooklm --version`
**Atualizar instalação gerenciada pela CLI:** `notebooklm skill install`
