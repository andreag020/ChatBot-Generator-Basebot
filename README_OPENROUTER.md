# Migración a OpenRouter

## Archivos reemplazados
- app/core/config.py
- app/core/ai_engine.py
- docker-compose.yml

## Archivo nuevo
- .env.openrouter.example

## Qué cambió
- Se agregó soporte para `AI_PROVIDER=openrouter`.
- El backend usa `POST /chat/completions` de OpenRouter con formato compatible con OpenAI.
- Se mantienen `ollama` y `anthropic` como proveedores opcionales, pero el `docker-compose.yml` ya no levanta Ollama.
- El proveedor por defecto quedó en `openrouter` con modelo `openrouter/free`.

## Configuración manual
1. Crea tu API key en OpenRouter.
2. Copia `.env.openrouter.example` como `.env`.
3. Completa `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID`, `WHATSAPP_VERIFY_TOKEN` y `OPENROUTER_API_KEY`.
4. Opcionalmente define `OPENROUTER_HTTP_REFERER` y `OPENROUTER_TITLE`.

## Modelos recomendados
- Gratis: `openrouter/free`
- Más estables cuando tengas saldo: usa cualquier modelo soportado por OpenRouter y colócalo en `OPENROUTER_MODEL` y `AI_MODEL`.

## Levantar el proyecto
```powershell
docker compose up -d --build
docker compose logs -f basebot
```

## Validaciones
- Salud del servicio:
```powershell
curl http://localhost:8001/health
```
- Debe mostrar `provider: openrouter` y el modelo configurado.

## Qué tomar en cuenta
- Si OpenRouter responde 401 o 403, revisa la API key.
- Si responde 400, revisa en logs el body exacto porque el código ya lo imprime.
- `openrouter/free` puede variar el modelo real usado por el router.
- Las cabeceras `HTTP-Referer` y `X-OpenRouter-Title` son opcionales.
