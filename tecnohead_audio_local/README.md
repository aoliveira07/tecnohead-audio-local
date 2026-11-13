# TecnoHead Áudio Local — App

Este diretório contém o manifesto Home Assistant, a imagem, o backend FastAPI, a UI Ingress e os testes. O fluxo administrativo é servido pelo porto interno 18199 e o stream de arquivos pelo processo `media_app` no porto 18198. Para reduzir pausas, o LinkPlay recebe uma playlist M3U local e administra a sequência das faixas no próprio dispositivo.

A configuração privada fica em `/data/config.json` com modo restrito. O índice fica em `/data/state/library.json`; downloads incompletos e originais usados para reprocessamento ficam em `/data/cache/downloads`. A coleção pronta e o M3U ficam em `/media/tecnohead-audio-local/library`.

Não coloque tokens no repositório, no `config.yaml`, em issues ou em capturas da interface.

