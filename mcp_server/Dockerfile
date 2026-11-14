FROM python:3.12-slim

WORKDIR /app

# Install MCP server dependencies
COPY mcp_server/requirements.txt mcp_server/requirements.txt
RUN pip install --no-cache-dir -r mcp_server/requirements.txt

# Copy MCP server code
COPY mcp_server/ mcp_server/

# Copy shared canonical mapping from the connector
COPY app/__init__.py app/__init__.py
COPY app/sta/__init__.py app/sta/__init__.py
COPY app/sta/canonical.py app/sta/canonical.py

EXPOSE 8030

CMD ["uvicorn", "mcp_server.server:app", "--host", "0.0.0.0", "--port", "8030"]
