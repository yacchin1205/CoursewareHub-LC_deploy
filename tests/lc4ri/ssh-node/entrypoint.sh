#!/bin/sh
set -eu
ssh-keygen -A
mkdir -p /home/centos/.ssh /run/sshd
cp /ci-key.pub /home/centos/.ssh/authorized_keys
chmod 0700 /home/centos/.ssh
chmod 0600 /home/centos/.ssh/authorized_keys
chown -R centos:centos /home/centos/.ssh
exec /usr/sbin/sshd -D -e
