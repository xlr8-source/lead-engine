"""
run_server.py - Run the FastAPI server with proper Windows support.
"""
import uvicorn
import asyncio
import sys

if __name__ == "__main__":
    # Force asyncio event loop on Windows
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    uvicorn.run(
        "api.main:app",
        host="127.0.0.1",
        port=8000,
        log_level="info",
        loop="asyncio",
    )