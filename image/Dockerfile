FROM niicloudoperation/notebook

# Keep server PID and connection files out of the shared user home.
ENV JUPYTER_RUNTIME_DIR=/tmp/jupyter-runtime

USER root

COPY --chmod=755 singleuser_healthcheck.py /etc/jupyter/docker_healthcheck.py

RUN python3 -m pip install --no-cache-dir awscli==1.46.2 passlib==1.7.4

USER $NB_USER
