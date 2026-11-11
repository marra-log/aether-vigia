# Agente: roda no local, perto das câmeras.
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vigia_agent ./vigia_agent
COPY models ./models
ENV VIGIA_MODEL=/app/models/yolox_nano.onnx
CMD ["python", "-m", "vigia_agent"]
