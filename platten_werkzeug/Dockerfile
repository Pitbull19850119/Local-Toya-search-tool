FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends e2fsprogs parted util-linux ntfs-3g exfatprogs rsync coreutils findutils procps \
 && rm -rf /var/lib/apt/lists/*
COPY run.sh /run.sh
RUN chmod +x /run.sh
CMD ["/run.sh"]
