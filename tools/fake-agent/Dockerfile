# A disposable SSH host with stand-in `claude` and `hermes` programs for testing ADM's SSH runner end to end.
#   docker build -t adm-fake-agent tools/fake-agent
#   docker run -d --name adm-fake --network host -e SSH_PORT=2222 -e AUTHORIZED_KEY="<ADM's public key>" adm-fake-agent
FROM debian:13-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server curl python3 python3-yaml \
 && rm -rf /var/lib/apt/lists/* && mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh
COPY fake_agent.py /usr/local/bin/claude
RUN ln -s /usr/local/bin/claude /usr/local/bin/hermes && chmod 755 /usr/local/bin/claude
CMD ["sh", "-c", "echo \"$AUTHORIZED_KEY\" > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys && ssh-keygen -A >/dev/null && exec /usr/sbin/sshd -D -e -p ${SSH_PORT:-2222} -o ListenAddress=127.0.0.1 -o PasswordAuthentication=no"]
