FROM rockylinux:9

ENV container=docker
RUN dnf install -y systemd openssh-server sudo passwd python3 util-linux iproute \
    && dnf clean all \
    && useradd -m centos \
    && passwd -d centos \
    && echo 'centos ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/centos \
    && chmod 440 /etc/sudoers.d/centos \
    && printf '\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' >> /etc/ssh/sshd_config \
    && systemctl enable sshd \
    && systemctl mask systemd-remount-fs.service \
    && rm -f /etc/machine-id
COPY entrypoint.sh /usr/local/bin/ci-systemd-entrypoint
RUN chmod +x /usr/local/bin/ci-systemd-entrypoint
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/local/bin/ci-systemd-entrypoint"]
