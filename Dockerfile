FROM ubuntu:24.04
LABEL authors="ender"
RUN apt-get update && apt-get install python3.12-full python3-pip -y
RUN python3.12 -m venv /venv
COPY . /app
WORKDIR /app
RUN /venv/bin/pip install -r /app/requirements.txt
ENTRYPOINT ["/venv/bin/python", "main.py"]