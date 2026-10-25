FROM alpine:3.21
RUN apk add --no-cache openssh-server python3 sudo \
    && adduser -D -s /bin/sh centos && passwd -d centos \
    && printf 'centos ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/centos \
    && chmod 0440 /etc/sudoers.d/centos \
    && printf '\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' >> /etc/ssh/sshd_config
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["sh", "/entrypoint.sh"]
