# Servidor (API, painel, IA de comportamento e alertas). É o serviço da Railway.
FROM python:3.13-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/app ./app
EXPOSE 8000
# Um processo só: o ciclo de alertas roda dentro dele e não pode duplicar.
CMD ["sh", "-c", "uvicorn app.main:app --host '::' --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
