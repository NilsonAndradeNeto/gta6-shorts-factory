# Fábrica de Shorts GTA 6

Sistema que pesquisa, verifica, roteiriza, narra, edita, valida e agenda no YouTube
Shorts verticais (1080×1920) sobre GTA VI, com custo R$ 0, rodando no GitHub Actions.

## Arquitetura

```
GitHub Actions (cron 6x/dia, sem servidor 24h)            branch `state` (SQLite)
 │                                                          ▲  snapshot a cada execução
 ├─ research/      RSS oficiais/imprensa/comunidade ────────┤  + backup diário (artefato)
 ├─ verification/  agrupa histórias → CONFIRMADO/REPORTADO/TEORIA/NÃO VERIFICADO
 ├─ editorial/     score (novidade, interesse, confiança, retenção, visual, diferença)
 │                 + pautas atemporais SÓ com texto oficial da Rockstar (reserva)
 ├─ scriptwriter/  LLM gratuito → roteiro JSON: cada fato tem evidência + citação literal
 ├─ verification/  checagem programática (citação existe? números? atribuição?) + juiz LLM
 ├─ voice/         Piper TTS pt-BR (local) frase a frase · reserva edge-tts
 ├─ subtitles/     faster-whisper: tempo por palavra + confirma que legenda = áudio
 ├─ storyboard/    cada frase → asset oficial marcado com o personagem/local citado
 ├─ assets/        kit oficial de mídia GTA VI (237 itens) + detecção facial p/ enquadrar
 ├─ renderer/      compositor OpenCV → FFmpeg: câmera, transições, legendas animadas,
 │                 trilha synthwave procedural + SFX próprios, loudness -14 LUFS
 ├─ qa/            técnico + visual + editorial; reprovado não sobe
 ├─ youtube/       OAuth2, upload resumível, publishAt (agendado), anti-duplicata, confirmação
 └─ analytics/     Data API + Analytics API → relatório por gancho/tipo/duração/horário
```

Estados de cada vídeo: `VERIFIED → SCRIPTED → AUDIO_READY → ASSETS_READY → RENDERED →
VALIDATED → UPLOADED → SCHEDULED → PUBLISHED`, com `FAILED` (retomável), `BLOCKED`
(reprovado, nunca publica) e `ABANDONED` (excedeu tentativas ou o horário passou).

## Limites reais das ferramentas gratuitas (verificados em 10/11/2026)

| Recurso | Limite | Uso estimado | Situação |
|---|---|---|---|
| GitHub Actions — repo **privado** (plano Free) | 2.000 min/mês | ~35 min/dia ≈ 1.050 min/mês (6 execuções + 4 renders) | Cabe, com folga de ~45% |
| GitHub Actions — repo **público** | minutos ilimitados em runners padrão | — | Alternativa se faltar minuto (código e banco ficam visíveis) |
| Duração máxima de job | 6 h | ~10–25 min | OK |
| Artefatos (Free) | 500 MB | vídeos 3 dias + banco 7 dias | OK (retenção curta) |
| Cache do Actions | 10 GB/repo | ~1,2 GB (kit oficial + vozes + whisper) | OK |
| YouTube `videos.insert` | doc atual: 1 unidade no balde "Video Uploads", 100 chamadas/dia | 4/dia | OK |
| YouTube — vídeos de projeto API não auditado | a referência atual de `videos.insert` diz que **não** ficam mais restritos a privado | — | O sistema detecta e alerta caso o vídeo fique preso como privado |
| OAuth em modo "Testing" | refresh token expira em 7 dias | — | **Publique o app OAuth em "In production"** (passo 3) |
| LLM | GitHub Models foi **descontinuado em 30/07/2026**. Gemini Flash-Lite (camada grátis ~500 req/dia) + Groq (~1.000 req/dia) | ~40 chamadas/dia | OK com as duas chaves gratuitas |
| TikTok | API só publica em **privado** sem auditoria, e as diretrizes vetam ferramentas para a própria conta | 4/dia | Entrega no Telegram às 09:00 → postagem em 1 toque |
| Geração de vídeo realista por IA | não há opção gratuita sustentável em CPU de runner | — | **Não usado**: editor cinematográfico automatizado com material oficial |

## Impedimentos e decisões

1. **Monetização × política da Rockstar.** A política permite uso **não comercial**. Ver
   [docs/LEGAL.md](docs/LEGAL.md). Monetizar é decisão sua; recomendo pedir autorização à Take-Two antes.
2. **Sem gameplay/trailers completos.** O kit oficial tem 228 imagens + 9 clipes curtos.
   Isso garante legalidade e correspondência semântica, mas limita a variedade visual,
   e o sistema evita repetir o mesmo asset nos vídeos recentes. Após o lançamento, a Rockstar
   costuma publicar novas imagens: basta incluir novos .zip em `factory/assets/catalog.py`.
3. **Notícias da Newswire.** A página é renderizada por JavaScript; sem navegador, só o
   parágrafo de abertura (og:description) é legível. Esse lead confirma o fato principal;
   os detalhes vêm da imprensa e são narrados como "segundo a <veículo>".
4. **Qualidade da voz.** Piper é natural para TTS offline, mas não chega ao nível de vozes
   comerciais. `edge-tts` soa melhor, porém é um endpoint não oficial (sem garantia); por isso
   fica como reserva. Troque com `TTS_PRIMARY=edge` se aceitar esse risco.
5. **4 vídeos/dia.** Tecnicamente sustentado. Mas se nenhuma pauta passar na checagem factual,
   o slot fica vazio: o sistema não publica conteúdo não verificado só para cumprir a meta.

## Configuração (uma única vez)

### 1. GitHub
O repositório e os workflows são criados pelo próprio assistente. Para rodar localmente:
```
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
```

### 2. Google Cloud (gratuito, sem cartão)
1. https://console.cloud.google.com → criar projeto (ex.: `gta6-shorts`).
2. APIs e serviços → Biblioteca → ativar **YouTube Data API v3** e **YouTube Analytics API**.
3. Tela de consentimento OAuth → tipo **Externo** → preencha nome/e-mail → adicione seu e-mail como usuário de teste.
4. **Publicar app → "Em produção"** (sem isso o token expira em 7 dias). Para uso próprio não é
   preciso concluir a verificação do Google; você verá o aviso "app não verificado" e pode prosseguir.
5. Credenciais → Criar ID do cliente OAuth → **App para computador** → baixar o JSON.

### 3. Autorizar o canal
```
.venv\Scripts\python -m factory.cli auth --client-secrets caminho\client_secret.json
```
Faça login **com a conta dona do canal**. O comando imprime `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`.

### 4. Secrets no GitHub
Repositório → Settings → Secrets and variables → Actions → *New repository secret*:
`YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` (opcionais: `GEMINI_API_KEY`, `GROQ_API_KEY`).
Para produzir sem enviar, crie a *variable* `FACTORY_DRY_RUN=1`.

### 5. Testar
- Actions → **factory** → *Run workflow* com `dry_run` marcado → baixe o artefato `output-*` e assista.
- Depois rode sem `dry_run`: os vídeos sobem **privados com publishAt** (agendados).

## Operação

| Comando | O que faz |
|---|---|
| `python -m factory.cli run` | ciclo completo (o que o Actions executa) |
| `python -m factory.cli research` / `topics` | coleta / lista pautas com score |
| `python -m factory.cli produce --topic ID [--upload]` | vídeo avulso de teste (não ocupa slots reais) |
| `python -m factory.cli status` / `dashboard` / `report --fetch` | acompanhamento |
| `python -m factory.cli youtube-check` | testa credenciais |

- **Painel:** `data/dashboard.html` no branch `state` (e resumo em cada execução do Actions).
- **Horários:** `config/settings.yaml → channel.publish_slots` (padrão 09:00, 12:30, 17:30, 21:00 BRT).
  Se mudar, ajuste também o cron em `.github/workflows/factory.yml` e `automation.cron_hours_utc`.
- **Fontes:** `config/sources.yaml`.

## Qualidade e verificações automáticas

- **Fatos:** citação literal obrigatória (fuzzy ≥ 90%), números checados contra a evidência,
  status do fato limitado pelo nível da fonte, atribuição obrigatória para REPORTADO,
  nomes próprios checados, juiz LLM independente (temperatura 0). Até 3 reescritas e depois troca de pauta.
- **Técnico:** 1080×1920, H.264/yuv420p, 30 fps, AAC, duração, decodificação completa,
  quadros pretos, silêncio, loudness, narração não cortada, cobertura de legenda,
  legenda = áudio (≥ 80% das palavras reconhecidas pelo Whisper).
- **Visual:** quadros uniformes/escuros, movimento contínuo (sem imagem parada ≥ 2 s), ritmo de cortes.
- **Editorial:** relação com GTA 6, originalidade contra 80 vídeos anteriores, licenças,
  confiança da correspondência visual, repetição de assets, título.
- **Limitações declaradas:** ritmo/suavidade são medidos por métricas indiretas, e a
  correspondência imagem↔fala depende das marcações oficiais do kit (pasta = personagem/local)
  e da detecção facial, não de "visão" quadro a quadro.

## Testes
```
.venv\Scripts\python -m pytest -q tests
```
