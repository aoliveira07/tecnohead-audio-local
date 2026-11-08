# Arquitetura

```text
Ingress 18199 ── FastAPI ── Runtime ──┬─ ConfigStore / StateStore (/data)
                                      ├─ LibraryManager (/media + índice)
                                      ├─ SyncService ── SpotDL ── LoudnessProcessor
                                      ├─ PlaybackSupervisor ── LinkPlayClient
                                      └─ Diagnostics / EventLog

AudioCast ──LAN HTTP 18198── media_app ── arquivo normalizado
```

## Persistência

- `/data/config.json`: defaults, playlist e credenciais privadas, modo 0600;
- `/data/state/`: índice de faixas, estado da sincronização, estado de playback, lock e eventos;
- `/data/cache/`: `playlist.spotdl` e originais para reprocessamento;
- `/media/tecnohead-audio-local/library/`: arquivos prontos e `playlist.m3u8`;
- `/media/tecnohead-audio-local/quarantine/`: área reservada para falhas futuras;
- `/media/tecnohead-audio-local/tmp/`: arquivos transitórios.

O índice é a fonte de verdade; a fila não depende de uma listagem de diretório. Caminhos derivados são normalizados e validados contra o diretório gerenciado. A promoção do normalizado usa arquivo temporário no mesmo volume e `os.replace`.

## Fluxos

1. `SpotDLAdapter` salva os metadados `.spotdl`, calcula diff por identificador estável e executa `spotdl sync` com argumentos estruturados. O CLI spotDL é instalado com `--no-deps`, depois das dependências seguras do App, porque a interface web opcional do spotDL 4.4.3 declara versões antigas de FastAPI/Uvicorn que não são usadas pela integração.
2. Cada arquivo é copiado para o cache controlado, validado por `ffprobe`, medido com `loudnorm` e convertido em arquivo final.
3. A faixa só vira `ready` depois de `ffprobe` confirmar stream, duração e formato do resultado.
4. O supervisor lê a janela com `ZoneInfo`, confirma dispositivos, reagrupa quando necessário, reaplica volume e toca URL local.
5. Fora da janela, o supervisor não inicia reprodução e encerra uma sessão ativa de modo determinístico.

## Ingress e stream

O frontend usa URLs relativas e não conhece o prefixo Ingress. O porto 18199 não aparece em `ports` e não é publicado na LAN. O porto 18198 é uma aplicação FastAPI separada que só possui `GET /stream/{entry_id}` com token interno e arquivo `ready`; qualquer outra rota devolve 404.

Como o Supervisor não conhece automaticamente o IP LAN adequado para cada AudioCast, `media.base_url` é editável e inicia vazio. A porta publicada é fixa em 18198; somente o host da URL é configurável. Sem essa configuração, o playback não inicia e a UI mostra a pendência.

Credenciais opcionais do Spotify são gravadas pelo endpoint Ingress `/api/credentials`; o formulário é write-only, campos vazios preservam o valor existente e a resposta contém somente flags `configured`. O `spotDL` recebe os valores apenas no ambiente do subprocesso.

