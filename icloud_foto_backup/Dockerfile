FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends cifs-utils tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "icloudpd==1.33.1" "flask>=3,<4" "waitress>=3" "requests>=2.31"

ENV PYTHONUNBUFFERED=1 TZ=Europe/Berlin
WORKDIR /app
COPY app/ /app/
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
