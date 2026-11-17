FROM python:3.12-slim

WORKDIR /app

# Install deps first (better layer caching — only re-runs if requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server
COPY ghost_blog_mcp.py .

# Run unprivileged (the container env carries a Ghost admin key)
RUN useradd --create-home mcp && chown -R mcp:mcp /app
USER mcp

# Streamable HTTP is the default transport for container deployments
ENV MCP_TRANSPORT=streamable-http
ENV PORT=8000

EXPOSE 8000

# slim has no curl; a python socket check that honors $PORT
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('PORT', 8000))), 3)"

CMD ["python", "ghost_blog_mcp.py"]
