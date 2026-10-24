FROM niicloudoperation/notebook

# Keep server PID and connection files out of the shared user home.
ENV JUPYTER_RUNTIME_DIR=/tmp/jupyter-runtime

USER root

COPY --chmod=755 singleuser_healthcheck.py /etc/jupyter/docker_healthcheck.py

RUN mamba install --quiet --yes awscli passlib && mamba clean --all -f -y

USER $NB_USER
