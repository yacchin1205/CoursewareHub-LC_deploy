#!/bin/sh
set -eu
ssh-keygen -A
mkdir -p /home/centos/.ssh
cat /ci-authorized-key > /home/centos/.ssh/authorized_keys
chown -R centos:centos /home/centos/.ssh
chmod 700 /home/centos/.ssh
chmod 600 /home/centos/.ssh/authorized_keys
exec /sbin/init
