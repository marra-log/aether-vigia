# Instalar o agente no local

O agente precisa estar na mesma rede das câmeras, porque um servidor na nuvem não
alcança câmeras dentro da rede do cliente. Basta um mini PC com Linux ligado em
nobreak e com cabo de rede.

## Passo a passo

1. Instale Ubuntu Server no mini PC e copie a pasta `agent/` para ele.
2. Pegue o endereço do servidor e o `AGENT_TOKEN` configurado na Railway.
3. Rode:
   ```bash
   sudo VIGIA_SERVER=https://seu-servico.up.railway.app AGENT_TOKEN=seu-token bash instalar.sh
   ```
4. Cadastre as câmeras na aba Câmeras do painel. O agente busca a lista sozinho a cada 30 segundos.

## Endereços das câmeras

Use o **fluxo secundário** (substream). Ele tem resolução suficiente para a análise e
pesa muito menos na CPU e na rede.

| Marca | Endereço |
|---|---|
| Intelbras ou Dahua | `rtsp://usuario:senha@IP:554/cam/realmonitor?channel=1&subtype=1` |
| Hikvision | `rtsp://usuario:senha@IP:554/Streaming/Channels/102` |

Crie na câmera um usuário só de visualização e mantenha o relógio ligado na imagem: ele
ajuda a IA a separar cena parada de imagem travada.

## Sem computador no local

Só dá para analisar da nuvem se as câmeras estiverem acessíveis pela internet. Abrir a
porta RTSP do roteador expõe câmera e senha, por isso não é recomendado. Prefira o
mini PC ou uma VPN.

## Desempenho medido

Em 2 vCPUs, a análise de cada imagem levou 3 ms e a detecção de pessoas 13 ms. A maior carga
real é decodificar o vídeo, então use o fluxo secundário e confirme o limite de câmeras
no equipamento escolhido.

## Se a internet cair

O agente guarda ocorrências e métricas e envia tudo quando a conexão volta. O servidor
percebe a ausência do agente em 2 minutos e avisa o dono.
