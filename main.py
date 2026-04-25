#!/usr/bin/env python3
"""
Main entry point that runs the API server independently.
The Discord bot is optional and won't crash the API if it fails.
"""

import os
import time
import threading
from api import run_api

def start_bot_safely():
    """Try to start the Discord bot in a separate thread. If it fails, log and continue."""
    try:
        # Import and start bot
        from bot import start_bot
        start_bot()
        print("✅ Discord bot started successfully")
    except Exception as e:
        print(f"❌ Discord bot failed to start: {e}")
        print("🌐 API server will continue running without Discord bot")

def main():
    print("🚀 Starting Vyron Services...")
    
    # Start API server in main thread
    print("🌐 Starting API server...")
    
    # Start bot in background thread (optional)
    if os.environ.get("TOKEN"):
        print("🤖 Starting Discord bot in background...")
        bot_thread = threading.Thread(target=start_bot_safely, daemon=True)
        bot_thread.start()
        
        # Give bot a moment to start
        time.sleep(3)
    else:
        print("⚠️ No TOKEN found - running API only")
    
    # Run API server (this will block and keep the service alive)
    print("🌐 API server running on main thread...")
    try:
        run_api()
    except KeyboardInterrupt:
        print("🛑 Shutting down...")
    except Exception as e:
        print(f"❌ API server error: {e}")

if __name__ == "__main__":
    main()
