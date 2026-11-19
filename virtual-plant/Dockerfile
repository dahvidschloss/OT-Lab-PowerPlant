FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY plant.py protocol.py server.py client.py terminal.py grid.py gateway.py gateway.json ./
COPY drivers ./drivers
USER 65534:65534
EXPOSE 20000
CMD ["python", "server.py", "--dnp-host", "0.0.0.0"]
