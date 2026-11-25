FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends cifs-utils tzdata \
 && rm -rf /var/lib/apt/lists/*

# icloudpd von PyPI ist nur ein fertiges Programm ohne Python-Module.
# ohne_album.py braucht die Module, deshalb aus dem Quellcode installieren.
RUN pip install --no-cache-dir "icloudpd @ https://github.com/icloud-photos-downloader/icloud_photos_downloader/archive/refs/tags/v1.33.1.tar.gz" "flask>=3,<4" "waitress>=3" "requests>=2.31" \
 && python -c "import icloudpd.authentication, pyicloud_ipd.services.photos" \
 && icloudpd --version

ENV PYTHONUNBUFFERED=1 TZ=Europe/Berlin
WORKDIR /app
COPY app/ /app/
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
