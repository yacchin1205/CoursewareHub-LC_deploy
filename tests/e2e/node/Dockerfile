FROM docker:27.5.1-dind

RUN apk add --no-cache openssh python3 sudo \
    && adduser -D -s /bin/sh centos \
    && passwd -d centos \
    && echo 'centos ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/centos \
    && chmod 440 /etc/sudoers.d/centos \
    && printf '\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' >> /etc/ssh/sshd_config

COPY entrypoint.sh /usr/local/bin/ci-node-entrypoint
RUN chmod +x /usr/local/bin/ci-node-entrypoint
ENTRYPOINT ["ci-node-entrypoint"]
