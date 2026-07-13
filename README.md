# AVOCarbon Costing Backend and MCP



Combined FastAPI backend and PostgreSQL MCP server for AVOCarbon Costing.



## Features



- REST API for the React frontend
- Choke sequential workflow and final calculation
- Azure Blob drawing upload
- MCP endpoint and costing tools
- Read costing database

- Read BOM

- Read routing

- Read supplier offers

- Read material prices

- Update costing records



## Local combined run



```bash

pip install -r requirements.txt

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

```



This is the production entrypoint. It serves REST, OpenAPI, legacy UI, and MCP from one process and one workflow-state path.

Running `python server.py` remains supported for MCP-only diagnostics, but it does not expose the React REST API.

## Azure App Service startup command

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Do not use `python server.py` for the combined Azure deployment.

## Health



http://localhost:8000/health



## REST API documentation

http://localhost:8000/docs

## MCP endpoint



http://localhost:8000/mcp

