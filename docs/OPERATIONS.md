# Instalação, operação, backup e rollback

## Primeira instalação

1. Faça backup do Home Assistant e confirme espaço disponível em `/media`.
2. Adicione o repositório do App no Supervisor e instale a versão desejada.
3. Abra o Ingress e configure a playlist.
4. A playlist pública não exige Client ID/Secret do Spotify. Se o broker MQTT exigir autenticação, informe usuário e senha na seção MQTT; a senha nunca é exibida novamente.
5. Informe os IPs/nomes dos AudioCast e volume máximo; os defaults são Caixa `192.168.0.13` e Restaurante `192.168.0.14`, ambos 90%.
6. Informe a URL LAN do stream, como `http://192.168.0.10:18198`. Esse é o IP do host HA visível pelos AudioCast, não o URL do Ingress.
7. Execute diagnóstico individual, grupo, sincronização manual e reprodução com volume seguro.
8. Depois da homologação, habilite a supervisão automática.

## Atualização

Use a atualização normal do Supervisor para uma tag publicada. `/data` e o mapeamento `/media` não devem ser removidos. Confira o changelog, faça backup antes, abra o Ingress e rode diagnóstico após a atualização. Reprocessamento é explícito; uma atualização não baixa a playlist novamente por si só.

## Backup e restore

- Inclua o App no backup do Home Assistant para preservar `/data/config.json`, índice, estados e cache.
- Faça uma cópia separada de `/media/tecnohead-audio-local` para preservar a biblioteca grande; não dependa de um snapshot do HA sem confirmar que o mapeamento `media` foi incluído.
- Restaure primeiro o backup do App, depois a pasta da biblioteca no mesmo caminho. Inicie o App, verifique `library.json`, permissões e espaço, e execute diagnóstico antes de reproduzir.
- Não exporte `config.json` para tickets ou repositórios: ele contém credenciais.

## Rollback

1. Pare o App pelo Supervisor e preserve `/data` e `/media/tecnohead-audio-local`.
2. Selecione a tag/release anterior no repositório do App ou restaure o backup do App.
3. Inicie a versão anterior e confirme diagnóstico, índice e um teste manual de grupo.
4. Se a versão anterior tiver um schema incompatível, restaure o `config.json` correspondente; não apague a biblioteca para corrigir uma falha de UI.

O rollback do App não altera automações da Padaria Imigrantes, AdGuard ou outras integrações.

