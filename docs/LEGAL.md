# Direitos autorais, políticas e riscos

> Isto não é aconselhamento jurídico. É o levantamento técnico que orientou as decisões do software.

## 1. Material da Rockstar Games

**Fonte visual usada:** exclusivamente o kit oficial de mídia de GTA VI
(`rockstargames.com/VI/media`), que a própria Rockstar apresenta com o texto
*"Download and share official videos, screenshots, and more"*, e as imagens que acompanham
os posts da Rockstar Newswire. Nada é baixado do YouTube nem de outros canais.

**Política da Rockstar** ("Policy on posting copyrighted Rockstar Games material",
support.rockstargames.com, atualizada em jan/2025, segundo a cobertura da imprensa; a página
oficial não carregou nas tentativas automatizadas):

- Permite publicar material dos jogos **em base não comercial**. "Não comercial" é definido
  como não ganhar dinheiro com o material postado nem usá-lo para promover produto/serviço.
- Vídeos com o final do jogo, cutscenes puras ou "grandes revelações" da história são removidos.
- Material de cópias vazadas/pré-lançamento é removido.
- A Take-Two se reserva o direito de remover qualquer material a qualquer momento.
- Dúvidas: copyright@take2games.com.

**Consequência prática — decisão que é sua:**

| Cenário | Risco |
|---|---|
| Canal **sem monetização** | Alinhado à política (uso não comercial, com crédito, comentário original). |
| Canal **monetizado** (YPP) | A política não autoriza explicitamente. Narração original + análise + edição substantiva fortalecem uma defesa de uso transformativo/comentário, mas a Take-Two pode reivindicar ou derrubar vídeos. Recomendo pedir autorização por e-mail (copyright@take2games.com) antes de ativar a monetização. |

**O que o sistema faz para reduzir o risco:**
- Narração 100% original (roteiro próprio) e análise com fontes citadas.
- Nunca usa cutscenes completas ou trechos longos: os clipes oficiais têm de 1 a 1,5 s e as imagens recebem edição.
- Crédito "Imagens: Rockstar Games" na tela durante todo o vídeo e na descrição.
- Aviso na descrição: canal de fãs, sem vínculo com Rockstar/Take-Two.
- Após o lançamento (19/11/2026), a regra de não mostrar "grandes revelações" da história é
  seguida porque o sistema não usa gameplay nem cutscenes. Notícias sobre spoilers devem ser
  tratadas com cautela (o prompt do roteirista proíbe narrar finais e reviravoltas).

## 2. Música e efeitos sonoros

A trilha de fundo e os efeitos (whoosh/impacto) são **sintetizados pelo próprio software**
(`factory/renderer/audio.py`) a partir de osciladores e ruído. Não há amostras de terceiros,
nem músicas comerciais, nem bibliotecas com licença restritiva. Isso elimina reivindicações de Content ID por música.

## 3. Fontes tipográficas

Anton e Montserrat, ambas sob a SIL Open Font License 1.1 (arquivos de licença em `assets/fonts/`).

## 4. Voz

Piper TTS (código GPL-3.0 no pacote `piper-tts` 1.3+, executado como ferramenta; nada é
redistribuído). As vozes pt_BR vêm do repositório `rhasspy/piper-voices`; cada voz tem seu
`MODEL_CARD` com a licença do dataset, e convém revisá-lo antes do uso comercial. A reserva
`edge-tts` usa um serviço da Microsoft **não oficial** para esse fim; por isso não é o motor padrão.

## 5. YouTube

- Uso apenas da YouTube Data API v3 oficial. Nenhuma automação de navegador.
- Política de **conteúdo inautêntico / produzido em massa** (antigo "conteúdo repetitivo"):
  é o maior risco para a monetização de canais automatizados. Mitigações implementadas:
  pautas diferentes por vídeo, deduplicação de pautas, roteiros, ganchos e títulos;
  rodízio de estilos de gancho; trilha diferente por vídeo; bloqueio de vídeos parecidos no QA.
- Conteúdo alterado/sintético: a narração é de um narrador sintético fictício (não imita
  pessoa real) e as imagens oficiais não são alteradas de forma realista; por isso
  `containsSyntheticMedia=false` (configurável em `config/settings.yaml`).

## 6. Fontes de notícias

Somente RSS públicos e páginas públicas. Os textos dos artigos são usados como evidência
interna para checagem; o vídeo cita a fonte e não reproduz o artigo.
