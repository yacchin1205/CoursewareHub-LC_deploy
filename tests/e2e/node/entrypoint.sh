#!/bin/sh
set -eu
ssh-keygen -A
mkdir -p /home/centos/.ssh /run/sshd
cat /run/ci-authorized-key > /home/centos/.ssh/authorized_keys
chown -R centos:centos /home/centos/.ssh
chmod 700 /home/centos/.ssh
chmod 600 /home/centos/.ssh/authorized_keys
/usr/sbin/sshd -E /tmp/sshd.log
exec /usr/local/bin/dockerd-entrypoint.sh "$@"
