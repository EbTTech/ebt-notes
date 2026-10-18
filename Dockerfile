FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860
# Long timeout because big uploads are streamed to the speech service.
CMD ["gunicorn", "-b", "0.0.0.0:7860", "--workers", "1", "--threads", "8", "--timeout", "900", "app:app"]
