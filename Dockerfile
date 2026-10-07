# sap-mcp -- MCP server for the SAP OData connector (stdio transport).
#
# Glama builds this image and speaks MCP over stdio: the server starts and answers
# `initialize` + `tools/list` without needing a live SAP system. Point it at a real
# system via SAP_BASE_URL / SAP_APIKEY (or ~/.config/sap-connector/config.json) to
# use the tools; reads are open, writes stay dry-run until SAP_WRITE_ENABLED=1.
FROM python:3.12-slim

ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY . /app

# stdio transport (the --http mode needs a host/port and is not used by Glama).
ENTRYPOINT ["python", "/app/mcp_server.py", "--stdio"]
