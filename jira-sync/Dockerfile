FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends cron \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py .
COPY check_epic_implements.py .
COPY list_custom_fields.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# 08:00 and 20:00 UTC = 08:00 and 20:00 CET (UTC+1)
RUN echo "0 7,19 * * * root . /etc/environment; cd /app && python sync.py >> /var/log/jira-sync.log 2>&1" \
    > /etc/cron.d/jira-sync \
 && chmod 0644 /etc/cron.d/jira-sync

CMD ["/app/entrypoint.sh"]
