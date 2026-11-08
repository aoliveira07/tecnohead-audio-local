# Plano e evidências de testes

## Automatizados executados no workspace

Comando:

```powershell
$env:PYTHONPATH='..\.test-deps;tecnohead_audio_local'
python -m ruff check tecnohead_audio_local/app tecnohead_audio_local/tests
python -m pytest -q tecnohead_audio_local/tests
```

Cobertura implementada: configuração e redaction, paths/traversal, diff/duplicatas/remoção suave, parser e comandos loudnorm, atomicidade, LinkPlay com respostas simuladas, timeout, grupo/volume, janela e limites, lock persistente, sincronização parcial, API relativa e UI escapada.

## Empacotamento

O workflow `test.yml` valida o manifesto, instala dependências, executa lint/testes, roda `pip-audit`, constrói `linux/amd64` e `linux/arm64` sem publicar e executa um smoke test do Ingress e do limite do stream.

O workstation desta entrega não possui Docker nem FFmpeg/ffprobe/spotDL instalados. Portanto não há evidência local de imagem, processamento FFmpeg real ou container iniciado; a evidência de build será o run verificável do GitHub Actions após o repositório existir.

## Homologação necessária

Registrar data, versão, digest, firmware e capturas/logs redigidos para:

- instalação pelo Supervisor e abertura do Ingress;
- configuração da URL base de mídia;
- diagnóstico individual e grupo dos IPs `192.168.0.13` e `192.168.0.14`;
- playlist real, download, loudness e duração;
- agrupamento, 90%, loop, play/pause/next/previous;
- janela 05:30–21:30 e parada às 21:30;
- reinício, perda/retorno de cada dispositivo;
- backup/restore do `/data` e da biblioteca em `/media`;
- ausência de porta administrativa publicada.

Nenhum desses itens físicos é marcado como concluído nesta entrega local.

