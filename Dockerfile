FROM python:3.12-slim

WORKDIR /app

# Dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código del sync
COPY . .

# Cloud Run Job: ejecuta el sync y termina (sin servidor HTTP).
CMD ["python", "run_all.py"]
