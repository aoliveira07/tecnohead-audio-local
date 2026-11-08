# Documentação operacional

As instruções verificadas e as limitações estão em:

- `../docs/ARCHITECTURE.md` — componentes, persistência, Ingress e stream LAN;
- `../docs/TEST_PLAN.md` — testes automatizados, empacotamento e homologação física;
- `../docs/OPERATIONS.md` — instalação, atualização, backup, restore e rollback;
- `../docs/LINKPLAY.md` — comandos usados, hipótese de firmware e pendências.

O App segue o padrão TecnoHead de UI local, persistência atômica, redaction e documentação de evidência. A resposta de uma API simulada nunca é tratada como teste físico.

Credenciais opcionais são enviadas pelo painel ao endpoint write-only `/api/credentials`, armazenadas em `/data/config.json` com modo 0600 e retornadas apenas como estado configurado.

