import yaml
import httpx
import logging
from pathlib import Path
from app.core.config import settings

logger = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self):
        self.tools_config = self._load_tools()

    def _load_tools(self) -> list[dict]:
        path = Path(settings.TOOLS_PATH)
        if not path.exists():
            logger.warning(f"Tools config not found at {path}")
            return []
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data.get("tools", [])

    def get_openai_tools(self) -> list[dict]:
        openai_tools = []
        for tool in self.tools_config:
            properties = {}
            required = []
            for param in tool.get("parameters", []):
                properties[param["name"]] = {
                    "type": param.get("type", "string"),
                    "description": param.get("description", ""),
                }
                if param.get("enum"):
                    properties[param["name"]]["enum"] = param["enum"]
                if param.get("required", False):
                    required.append(param["name"])

            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
        return openai_tools

    async def execute(self, tool_name: str, args: dict) -> dict:
        tool = next((t for t in self.tools_config if t["name"] == tool_name), None)
        if not tool:
            return {"error": f"Tool '{tool_name}' not found"}

        endpoint = tool.get("endpoint", "")
        if endpoint.startswith("mock:"):
            return self._mock_response(tool_name, args)

        method = tool.get("method", "POST").upper()
        headers = tool.get("headers", {"Content-Type": "application/json"})
        timeout = tool.get("timeout", 8.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "GET":
                    resp = await client.get(endpoint, params=args, headers=headers)
                elif method == "POST":
                    resp = await client.post(endpoint, json=args, headers=headers)
                else:
                    return {"error": f"Unsupported method: {method}"}
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error(f"Tool {tool_name} error: {e}")
            return {"error": "No se pudo completar la accion"}

    def _mock_response(self, tool_name: str, args: dict) -> dict:
        import random

        if tool_name == "registrar_lead":
            lead_id = f"CVS-{random.randint(10000, 99999)}"
            return {
                "lead_id": lead_id,
                "estado": "registrado",
                "nombre": args.get("nombre"),
                "empresa": args.get("empresa"),
                "servicio": args.get("servicio_interes"),
                "mensaje": (
                    f"Gracias por su interes. Hemos registrado su solicitud (Ref. {lead_id}). "
                    f"Nuestro equipo comercial se comunicara con usted en las proximas 24 horas habiles."
                ),
            }

        if tool_name == "agendar_reunion":
            reunion_id = f"RNF-{random.randint(1000, 9999)}"
            return {
                "reunion_id": reunion_id,
                "estado": "agendada",
                "nombre": args.get("nombre"),
                "empresa": args.get("empresa"),
                "modalidad": args.get("modalidad", "Por definir"),
                "mensaje": (
                    f"Reunion registrada (Ref. {reunion_id}). "
                    f"Nuestro equipo comercial confirmara la fecha y hora exacta "
                    f"comunicandose al numero proporcionado."
                ),
            }

        return {"resultado": "ok", "args": args}
