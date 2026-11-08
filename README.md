# TecnoHead Áudio Local

App nativo do Home Assistant OS para manter uma biblioteca de áudio local, normalizar loudness com FFmpeg e reproduzir a fila nos AudioCast por LinkPlay. A interface é em português do Brasil, responsiva e acessível por Ingress. Não é uma integração HACS.

## Estado desta entrega

O código está implementado no workspace em `tecnohead-audio-local/`. O endereço GitHub de destino é `https://github.com/aoliveira07/tecnohead-audio-local`, mas a verificação em 08/11/2026 retornou `Repository not found`; publicação e digest GHCR ficam pendentes de criação/acesso ao repositório.

O App não altera automações da Padaria Imigrantes e não contém regras de AdGuard, bloqueio de Spotify Connect, AirPlay ou firewall.

## Recursos

- spotDL encapsulado sem shell e com lock persistente, sincronização manual e às 03:00/15:00 em `America/Sao_Paulo`;
- biblioteca em `/media/tecnohead-audio-local/library`, índice e cache em `/data`;
- FFmpeg `loudnorm` em duas passagens, alvo inicial `-18 LUFS` e `-2 dBTP`, com fallback dinâmico documentado;
- cliente LinkPlay com timeout/retry, leitura de estado, grupo, volume, loop, play/pause/next/previous e recuperação;
- janela operacional 05:30–21:30, sem reinício automático fora dela;
- Ingress administrativo no porto interno 18199 e stream LAN separado, somente mídia, em 18198;
- logs, diagnósticos, configurações e exportações redigidos;
- credenciais opcionais do Spotify configuráveis pelo Ingress, gravadas em `/data/config.json` com modo restrito e nunca devolvidas em texto;
- imagens `amd64` e `aarch64` publicáveis no GHCR.

## Instalação no Home Assistant OS

1. Faça um backup do Home Assistant.
2. Em **Configurações → Apps → Repositórios**, adicione `https://github.com/aoliveira07/tecnohead-audio-local` quando o repositório estiver publicado.
3. Instale **TecnoHead Áudio Local** e abra o painel Ingress.
4. Informe a playlist, os endereços dos AudioCast e a URL base de mídia acessível pela LAN, por exemplo `http://192.168.0.10:18198`.
5. Execute **Diagnóstico**, teste cada dispositivo e teste o grupo.
6. Execute uma sincronização manual e confirme faixas `ready` na biblioteca.
7. Homologue a reprodução manual; só depois deixe a supervisão automática habilitada.

A porta 18198 é publicada para os AudioCast buscarem arquivos. Ela não serve o painel nem a API; o painel continua somente no Ingress. A porta e a URL base precisam ser alcançáveis pelos IPs `192.168.0.13` e `192.168.0.14`.

## Operação e limites

O botão **Sincronizar agora** pode ser usado fora da janela de reprodução. Remoções da playlist viram `removed` e não apagam arquivos automaticamente. **Reprocessar biblioteca** usa o cache local e não baixa novamente enquanto o original persistir.

O comando LinkPlay somente é marcado como confirmado quando a resposta HTTP e, para agrupamento, o status do mestre confirmam a operação. Isso não comprova som audível. A API, o firmware, o formato de áudio e o comportamento do grupo dos dois AudioCast ainda precisam de homologação física.

Consulte:

- [DOCS.md](tecnohead_audio_local/DOCS.md)
- [Arquitetura](docs/ARCHITECTURE.md)
- [Plano de testes](docs/TEST_PLAN.md)
- [Operação, backup e rollback](docs/OPERATIONS.md)
- [Contrato LinkPlay e pendências físicas](docs/LINKPLAY.md)

## Desenvolvimento

```powershell
cd tecnohead_audio_local
python -m pip install -r requirements.txt pytest pytest-asyncio ruff pyyaml
python -m ruff check app tests
python -m pytest -q
python tools/validate_app.py
```

O build multi-arquitetura é feito no GitHub Actions. Sem Docker local, não se deve declarar que a imagem foi construída no workstation.

