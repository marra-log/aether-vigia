# Aether Vigia

Monitoramento de câmeras com IA. O dono vê as oscilações e anomalias em um painel no
celular e recebe alertas pelo WhatsApp, com a imagem do momento.

## O que ele detecta

| Grupo | Ocorrências |
|---|---|
| Sinal e oscilações | Câmera fora do ar, imagem travada, imagem preta, desfoque, imagem piscando, conexão oscilando, várias câmeras caindo juntas, perda de contato com o local |
| Segurança | Pessoa em área e horário protegidos, pessoa parada por muito tempo, câmera tampada, câmera movida |
| Comportamento (IA) | Movimento muito acima ou muito abaixo do normal para aquele horário e tipo de dia |
| Operador | Anomalia apontada por uma pessoa da equipe, com observação e imagem |

## Como funciona

```
Câmeras ──RTSP──> Agente no local ──HTTPS──> Servidor na Railway ──> WhatsApp
                  analisa os quadros          aprende o normal       alerta com imagem
                  detecta pessoas             guarda o histórico     resumo diário
                                              painel do dono <────── comandos do dono
```

- **`agent/`** roda em um mini PC na rede das câmeras. Avalia cada imagem e usa o YOLOX-Nano
  (licença Apache-2.0, permite uso comercial) para achar pessoas e veículos. Se a internet
  cair, guarda os dados e envia depois.
- **`whatsapp/`** é o conector por QR Code (Baileys), para começar com o próprio número.
- **`server/`** é a API em FastAPI, com o painel, o banco e o motor de alertas.
- **IA de comportamento**: uma base por meia hora, separando dia útil de fim de semana,
  confirmada por uma Isolation Forest treinada por câmera. Cada câmera aprende por cerca
  de 2 dias antes de alertar. Marcar "alarme falso" deixa aquela câmera menos sensível.

## IA e operador trabalhando juntos

A IA vigia todas as câmeras o tempo todo e destaca o que foge do normal. O operador da
empresa entra no painel com uma senha própria e:

- **Revisa** o que a IA destacou: confirma e o dono recebe, ou descarta e o dono não é
  incomodado. Ligue com `OPERATOR_REVIEW=1`.
- **Aponta** uma anomalia que viu, com observação e gravidade. Ela vai ao WhatsApp do dono
  com a última imagem da câmera.

Alertas críticos (câmera fora do ar, tampada, invasão) nunca esperam revisão. Se o operador
não revisar em `REVIEW_TIMEOUT_MIN` minutos, o alerta segue com o aviso "não revisado".
Descartar um alerta de comportamento deixa aquela câmera menos sensível. O operador não
altera câmeras, números nem pausas.

## Menos alarme à toa

- Uma falha precisa persistir para virar ocorrência; a câmera tem 60 segundos para voltar antes de avisar.
- Se quase todas caem juntas, chega uma mensagem só: provável queda de energia ou internet.
- Quedas repetidas viram um único aviso de "sinal oscilando".
- Dia e infravermelho têm referências separadas, para a noite não virar alarme.
- Há intervalo mínimo entre alertas iguais, horário de não incomodar e aviso quando normaliza.

## Colocar no ar

1. **Servidor**: crie um projeto na Railway a partir deste repositório, adicione um Postgres
   e ligue a variável `DATABASE_URL` ao serviço.
2. **Variáveis**: copie de `.env.example`. As obrigatórias são `SECRET_KEY`, `OWNER_PASSWORD` e `AGENT_TOKEN`.
3. **Painel**: abra o endereço do serviço, entre com a senha, cadastre as câmeras e o número do dono.
4. **Agente**: instale no local seguindo [docs/instalacao-agente.md](docs/instalacao-agente.md).
5. **WhatsApp**: siga [docs/whatsapp.md](docs/whatsapp.md).

Antes de vender para clientes, leia [docs/lgpd.md](docs/lgpd.md).

Mantenha uma réplica só do servidor: o ciclo de alertas roda dentro do processo. Para
atender vários clientes, suba um servidor por cliente.

## Testar

```bash
pip install -r server/requirements.txt -r agent/requirements.txt pytest scikit-image
pytest -m "not slow"   # testes rápidos
pytest                 # inclui o teste longo da IA, cerca de 5 minutos
python tools/build_demo.py demo.html
```

## O que foi testado

Com dados sintéticos e o modelo real de detecção:

- Testes automáticos de sinal, detecção, IA, API, alertas, comandos e formato das chamadas
  aos três provedores de WhatsApp. Os testes de servidor também passaram em Postgres 16.
- Vídeo simulado de ponta a ponta, do agente ao alerta: as 7 falhas injetadas foram detectadas.
- IA em 6 semanas simuladas: 5 de 5 picos, 3 de 3 paradas e nenhum alarme falso em 21 dias.
- Velocidade em 2 vCPUs: 3 ms por análise de imagem e 13 ms por detecção de pessoas.

**Ainda precisa ser validado em campo**: câmera RTSP real, envio real por cada provedor,
descrição por IA com chave real e os builds de Docker. Dados simulados são mais regulares
do que a vida real, então espere ajustar os limites nas primeiras semanas.

## Créditos

YOLOX (Apache-2.0), OpenCV (Apache-2.0), scikit-learn (BSD), Schibsted Grotesk (OFL).
