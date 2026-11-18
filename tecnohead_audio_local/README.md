# TecnoHead Áudio Local — App

Este diretório contém o manifesto Home Assistant, a imagem, o backend FastAPI, a UI Ingress e os testes. O fluxo administrativo é servido pelo porto interno 18199 e o stream de arquivos pelo processo `media_app` no porto 18198. Para reduzir pausas, o LinkPlay recebe uma playlist M3U local e administra a sequência das faixas no próprio dispositivo.

A configuração privada fica em `/data/config.json` com modo restrito. O índice fica em `/data/state/library.json`; downloads incompletos e originais usados para reprocessamento ficam em `/data/cache/downloads`. A coleção pronta e o M3U ficam em `/media/tecnohead-audio-local/library`.

Não coloque tokens no repositório, no `config.yaml`, em issues ou em capturas da interface.

## Zonas AudioCast

O grupo multiroom aceita de 1 a 8 dispositivos LinkPlay habilitados. A primeira zona habilitada permanece como master e todas as demais entram no mesmo grupo e reproduzem a playlist local. O volume global é o alvo do master; cada zona recebe `min(alvo global, volume da zona)`, permitindo, por exemplo, Caixa/Restaurante em 90% e Varanda em 60%. Em upgrades, a zona `Varanda` é adicionada desabilitada para preservar o grupo existente; informe o IP ou DNS correto na aba **Configurações**, salve e só então marque **Habilitado**. O aplicativo não presume o endereço da Varanda.

O botão **Adicionar AudioCast** permite cadastrar outras zonas. Cada nova zona começa desabilitada, sem qualquer tentativa de conexão, até que um host válido seja informado e a configuração seja salva.

