FROM ubuntu:24.04
LABEL authors="ender"
RUN apt-get update && apt-get install python3.12-full -y
COPY . /app
WORKDIR /app
RUN pip3 install ./requirements.txt
ENTRYPOINT ["python3", "main.py"]