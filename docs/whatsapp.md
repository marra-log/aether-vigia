# Configurar o WhatsApp

Enquanto `WHATSAPP_PROVIDER=console`, nada é enviado: as mensagens só aparecem no log.
Defina o provedor quando for ligar de verdade e use **Testar** na aba WhatsApp do painel.

| Provedor | Quando usar | Cuidados |
|---|---|---|
| `baileys` | Início, com o próprio número, via QR Code | Não oficial. O WhatsApp pode bloquear o número. Use um chip separado. |
| `meta` | Produto para clientes | Exige conta comercial e modelos aprovados. Cada alerta é cobrado. |
| `evolution` | Teste rápido, via QR Code | Não oficial. O WhatsApp pode bloquear o número. Precisa hospedar a Evolution. |
| `zapi` | Teste rápido, sem hospedar nada | Não oficial, com o mesmo risco de bloqueio. Mensalidade da Z-API. |

## QR Code com o conector próprio (Baileys)

A pasta `whatsapp/` é um serviço pequeno que mantém a sessão do WhatsApp e fala só com o servidor.

1. Suba `whatsapp/` como um segundo serviço, com um volume em `/data` (é onde fica a sessão).
2. No conector: `GATEWAY_KEY` (chave longa), `PORT=3000` e
   `WEBHOOK_URL=http://ENDERECO-INTERNO-DO-SERVIDOR/webhook/whatsapp/SEU-SEGREDO`.
3. No servidor: `WHATSAPP_PROVIDER=baileys`, `BAILEYS_URL=http://ENDERECO-INTERNO-DO-CONECTOR:3000`,
   `BAILEYS_KEY` igual à `GATEWAY_KEY` e `WHATSAPP_WEBHOOK_SECRET=SEU-SEGREDO`.
4. Abra a aba WhatsApp do painel e leia o QR Code em **Dispositivos conectados**.

O conector não precisa de endereço público. Se o celular desconectar a sessão, o painel volta
a mostrar o QR Code e os alertas ficam na fila, tentando de novo.

Por ser uma conexão não oficial, use um número só para os alertas, envie apenas para quem
pediu para receber e migre para a API da Meta quando for vender em escala.

## API oficial da Meta

1. Crie um app em developers.facebook.com, adicione o produto WhatsApp e registre o número.
2. Gere um token permanente de usuário do sistema.
3. Preencha no servidor: `META_TOKEN`, `META_PHONE_NUMBER_ID`, `META_APP_SECRET` e um
   `META_VERIFY_TOKEN` inventado por você.
4. Em Webhooks, use `https://SEU-SERVIDOR/webhook/whatsapp` com o mesmo token de
   verificação e assine o campo `messages`.
5. Crie os cinco modelos abaixo, categoria **Utilidade**, idioma **Português (BR)**.

A Meta só permite que a empresa inicie conversa por modelo aprovado, por isso os alertas
sempre usam modelos.

### `vigia_alerta_imagem`
Cabeçalho: imagem. Botões de resposta rápida, nesta ordem: `Ciente` e `Silenciar 1h`.
```
Alerta: {{1}}
Local: {{2}}
Horário: {{3}}
{{4}}
Toque em um botão ou responda status para ver a situação.
```

### `vigia_alerta`
Igual ao anterior, sem cabeçalho de imagem, com os mesmos botões.

### `vigia_resolvido`
```
Normalizado: {{1}} em {{2}}. Voltou ao normal depois de {{3}}. Nenhuma ação é necessária.
```

### `vigia_resumo`
```
Resumo das últimas 24h em {{1}}.
Câmeras no ar: {{2}} do tempo.
Ocorrências: {{3}}, sendo {{4}} críticas.
Comportamento fora do normal: {{5}}.
Câmera com mais ocorrências: {{6}}.
Em aberto agora: {{7}}.
Os detalhes estão no painel.
```

### `vigia_teste`
```
Teste do Aether Vigia em {{1}}: os alertas vão chegar neste número.
```

## Evolution API

Preencha `EVOLUTION_URL`, `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCE` e invente um
`WHATSAPP_WEBHOOK_SECRET`. Na instância, aponte o webhook de `MESSAGES_UPSERT` para
`https://SEU-SERVIDOR/webhook/whatsapp/SEU-SEGREDO`.

## Z-API

Preencha `ZAPI_INSTANCE`, `ZAPI_TOKEN`, `ZAPI_CLIENT_TOKEN` e `WHATSAPP_WEBHOOK_SECRET`.
No webhook "Ao receber", use o mesmo endereço da Evolution.

## O que dá para responder no WhatsApp

Só números cadastrados no painel podem dar comandos.

| Mensagem | O que faz |
|---|---|
| `status` | Mostra as câmeras e o que está em aberto |
| `ok 12` | Marca a ocorrência 12 como vista |
| `falso 12` | Marca como alarme falso; em comportamento, deixa a câmera menos sensível |
| `silenciar 2h` | Pausa os avisos; invasão e câmera tampada continuam chegando |
| `reativar` | Volta a avisar |

Com a revisão do operador ligada, `ok 12` também libera um alerta que ainda esperava revisão,
e `falso 12` o descarta.
