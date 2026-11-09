# Documentação operacional

As instruções verificadas e as limitações estão em:

- `../docs/ARCHITECTURE.md` — componentes, persistência, Ingress e stream LAN;
- `../docs/TEST_PLAN.md` — testes automatizados, empacotamento e homologação física;
- `../docs/OPERATIONS.md` — instalação, atualização, backup, restore e rollback;
- `../docs/LINKPLAY.md` — comandos usados, hipótese de firmware e pendências.

O App segue o padrão TecnoHead de UI local, persistência atômica, redaction e documentação de evidência. A resposta de uma API simulada nunca é tratada como teste físico.

O acesso à playlist usa a página/embed público e não exige credenciais do Spotify. A senha opcional do broker MQTT é enviada pelo painel ao endpoint write-only `/api/mqtt/credentials`, armazenada em `/data/config.json` com modo 0600 e retornada apenas como estado configurado. O App publica por MQTT Discovery o botão `Sincronizar músicas` e escuta `tecnohead/audio_local/sync/set` por padrão.

