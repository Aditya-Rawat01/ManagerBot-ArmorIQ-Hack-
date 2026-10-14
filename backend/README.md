# ProxyLead backend

FastAPI application. Owns trusted requester identity, request orchestration,
the ArmorIQ gateway, and response/delivery routing. It must never query
protected knowledge tables directly; protected reads go through ArmorIQ and
the MCP tool server.
