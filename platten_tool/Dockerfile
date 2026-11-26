FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends parted e2fsprogs util-linux ntfs-3g rsync udev findutils cifs-utils tzdata && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "flask>=3,<4" "waitress>=3" "requests>=2.31"
ENV TZ=Europe/Berlin
COPY main.py /main.py
CMD ["python", "-u", "/main.py"]
