FROM python:3.12-slim
RUN pip install --no-cache-dir "cryptography>=42" "requests>=2.31"
COPY app.py /app.py
ENV PYTHONUNBUFFERED=1
CMD ["python3", "/app.py"]
