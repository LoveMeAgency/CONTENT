FROM python:3.11.3-alpine

# Timezone & certificats
RUN apk add --no-cache ca-certificates tzdata && update-ca-certificates

WORKDIR /app

COPY requirements.txt /app/
RUN pip3 install --no-cache-dir -r requirements.txt

# Dossiers persistables pour DB & session Pyrogram
RUN mkdir -p /app/database /app/session

COPY . /app/

ENV TZ=Europe/Malta

CMD ["python3", "bot.py"]
