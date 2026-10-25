FROM niicloudoperation/notebook@sha256:2823b779674971aff907016f0c6f640b21f99a712bb05842fa556d8e53b2ed8a
USER root
RUN python3 -m pip uninstall -y ansible \
 && python3 -m pip install --no-cache-dir ansible==9.13.0 ansible-core==2.16.14 jupyter-server==1.24.0 \
    && mkdir -p /home/jovyan/work /home/jovyan/.ssh \
    && chown -R 1000:100 /home/jovyan/work /home/jovyan/.ssh
USER $NB_USER
