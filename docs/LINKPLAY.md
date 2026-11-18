# LinkPlay / AudioCast

O cliente usa HTTP GET em `http(s)://<host>:<port>/httpapi.asp?command=...` com timeout e retry limitados. Os comandos padrão implementados são:

- `getPlayerStatus`, `getStatus`;
- `setPlayerCmd:play:<URI>`, `pause`, `resume`, `stop`, `next`, `prev`;
- `setPlayerCmd:vol:<0-100>`, `setPlayerCmd:slave_vol:<0-100>`;
- `setPlayerCmd:loopmode:0|1|2`;
- `multiroom:getSlaveList`, `multiroom:Ungroup`;
- `ConnectMasterAp:JoinGroupMaster:eth<master_ip>:wifi0.0.0.0` enviado ao membro.

Após um `getSlaveList` que ainda não confirma algum membro, o App reseta
somente os membros ausentes (`multiroom:Ungroup` e `setMultiroomLogic:1`) e
repete o ingresso, sempre exigindo nova confirmação do mestre. O volume efetivo
do grupo é limitado pelo menor limite configurado entre as zonas.

Esses comandos são uma hipótese configurável baseada na API LinkPlay pública e na integração LinkPlay do Home Assistant. Firmware presente nos AudioCast pode usar semântica ou transporte diferente, inclusive HTTPS. O App não registra tokens em URL de logs e não informa sucesso quando há timeout ou quando o read-back do grupo não confirma o membro.

## Teste físico pendente

É necessário confirmar nos dois aparelhos: porta/protocolo, identidade/modelo/firmware, semântica de `JoinGroupMaster`, resposta de `getSlaveList`, URL HTTP local, formato suportado, volume 90%, loop e recuperação após perda de rede. Enquanto isso, testes unitários usam respostas simuladas e ficam identificados como tal.

O retorno da API não comprova som audível. O teste deve registrar observação física separadamente.

